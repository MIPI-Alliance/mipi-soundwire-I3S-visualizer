"""
Copyright (c) 2019 Apple Inc. All Rights Reserved.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

# To Do
# Fix guard bits when using channel grouping. Should be at the end of each channel group? Or after the last bitslot driven in each row.
# Add G polarity
# Add phat bits


# Done
# Don't draw guard or tail bit for a sink data port
# Fixed an issue where guard bitslots depended on tail length parameter
# Looks like already had a CDS guard bit option

from __future__ import division  # Fixes a Python 2 issue with integer division

import sys
import io
import math
import csv
import os
import platform
import distutils.util

try:
    import canvasvg
except ImportError:
    canvasvg = None
    print('\nNote: Adding canvasvg module to your environment will enable SVG export\n')

if sys.version_info[0] == 3:
    # For Python 3
    print('Python 3.x')
    import tkinter as tk
    from tkinter import messagebox
    import tkinter.filedialog as filedialog

    to_unicode = str
else:
    # For Python 2
    print('Python 2.x')
    import Tkinter as tk
    import tkMessageBox as messagebox
    import tkFileDialog as filedialog
    import fractions

    to_unicode = unicode


class App(tk.Frame):
    def __init__(self, master):
        tk.Frame.__init__(self, master)
        # root is window

        self.VERSION = '1.16'

        # Appearance settings
        self.NUMBER_ENTRY_WIDTH = 5  # In characters
        self.ROW_SIZE = 30  # In pixels
        self.COLUMN_SIZE = 35  # In pixels
        self.TEXT_SIZE = 12  # In points
        self.ENTRYBOX_RELIEF = tk.SOLID

        if platform.system() == 'Windows':
            self.TEXT_SIZE -= 3
            self.COLUMN_SIZE = 33  # In pixels
            self.NUMBER_ENTRY_WIDTH = 6  # In characters

        self.AUX_CANVAS_FUDGE = 7  # Aligns header with main canvas
        self.PREFERRED_GRAY = '#ececec'
        self.DARK_GRAY = '#8f8f8f'
        self.LIGHT_GRAY = '#bfbfbf'
        self.APP_FONT = 'TkDefaultFont'

        # Frame default parameters
        self.MIN_ROWS_IN_FRAME = 1
        self.MAX_ROWS_IN_FRAME = 10240
        self.rows_in_frame = 64

        self.DP_COLORS = ['#FF80BF', '#FFA080', '#FFFF80', '#A0FF80', '#80FFFF', '#8080FF', '#BF80FF', '#FFBFFF', '#FFBFBF', '#FFFFBF',
                          '#BFFFBF', '#BFFFFF']

        self.INTERFACE_PARAMETER_TITLES = ['Rows to Draw (' + str(self.MIN_ROWS_IN_FRAME) + '-' + str(self.MAX_ROWS_IN_FRAME) + ')',
                                           'Columns per Row (' + str(Interface.MIN_COLUMNS_PER_ROW) + '-' + str(Interface.MAX_COLUMNS_PER_ROW)
                                           + ')',
                                           'S0 S1 Enabled',
                                           'S0 Width (' + str(Interface.MIN_S0_WIDTH) + '-' + str(Interface.MAX_S0_WIDTH) + ')',
                                           'S0 Handover Enabled',
                                           'CDS Guard Enabled',
                                           'Handover Width (' + str(Interface.MIN_HANDOVER_WIDTH) + '-' + str(
                                               Interface.MAX_HANDOVER_WIDTH) + ')',
                                           'Tail Width (' + str(Interface.MIN_TAIL_WIDTH) + '-' + str(Interface.MAX_TAIL_WIDTH) + ')',
                                           'Interval Denominator (' + str(Interface.MIN_INTERVAL_DENOMINATOR) + '-' + str(
                                               Interface.MAX_INTERVAL_DENOMINATOR) + ')',
                                           'Bulk Horizontal Start (1-' + str(Interface.MAX_COLUMNS - 2) + ')',
                                           'Bulk Width (0,2,5,10)',
                                           'Bulk Guard Enabled',
                                           'Row Rate [kHz] (' + str(Interface.MIN_ROW_RATE) + '-' + str(Interface.MAX_ROW_RATE) + ')']

        self.DP_PARAMETER_DESCRIPTIONS = ['Channels (' + str(DataPort.MIN_CHANNELS) + '-' + str(DataPort.MAX_CHANNELS) + ')',
                                          'Channel Grouping (' + str(DataPort.MIN_CHANNEL_GROUPING) + '-' + str(
                                              DataPort.MAX_CHANNEL_GROUPING) + ')',
                                          'Channel Group Spacing (' + str(DataPort.MIN_CHANNEL_GROUP_SPACING) + '-' + str(
                                              DataPort.MAX_CHANNEL_GROUP_SPACING) + ')',
                                          'Sample Width (' + str(DataPort.MIN_SAMPLE_WIDTH) + '-' + str(DataPort.MAX_SAMPLE_WIDTH) + ')',
                                          'Sample Grouping (' + str(DataPort.MIN_SAMPLE_GROUPING) + '-' + str(
                                              DataPort.MAX_SAMPLE_GROUPING) + ')',
                                          'Interval (' + str(DataPort.MIN_INTERVAL_INTEGER) + '-' + str(
                                              DataPort.MAX_INTERVAL_INTEGER) + ')',
                                          'Fractional Interval (' + str(DataPort.MIN_INTERVAL_NUMERATOR) + '-' + str(
                                              DataPort.MAX_INTERVAL_NUMERATOR) + ')',
                                          'Offset (' + str(DataPort.MIN_OFFSET) + '-' + str(DataPort.MAX_OFFSET) + ')',
                                          'Horizontal Start (0-' + str(Interface.MAX_COLUMNS - 1) + ')',
                                          'Horizontal Stop (0-' + str(Interface.MAX_COLUMNS - 1) + ')',
                                          'Source [checked] / Sink',
                                          'Handover Enabled',
                                          'Tail Enabled',
                                          'Guard Enabled',
                                          'Data Port Enabled',
                                          'Calculated Sample Rate [kHz]']

        window_width = int(35.5 * self.COLUMN_SIZE)
        window_height = 800
        data_frame_height = 400
        canvas_height = 200
        self.canvas_width = int(35.5 * self.COLUMN_SIZE)

        self.interface = Interface()

        self.master.title('SoundWire Next Payload Visualizer v' + self.VERSION)
        self.master.minsize(window_width, window_height)
        self.master.geometry("+150+50")
        self.master.resizable(False, True)
        self.master.tk_setPalette(background=self.PREFERRED_GRAY)
        self.master.config(menu=tk.Menu(self.master))

        # Used to validate data port entry widget values
        self.channels_vcmd = (self.register(self.validate), '%d', '%P', DataPort.MIN_CHANNELS, DataPort.MAX_CHANNELS)
        self.channel_grouping_vcmd = (
            self.register(self.validate), '%d', '%P', DataPort.MIN_CHANNEL_GROUPING, DataPort.MAX_CHANNEL_GROUPING)
        self.channel_group_spacing_vcmd = (self.register(self.validate), '%d', '%P', DataPort.MIN_CHANNEL_GROUP_SPACING,
                                           DataPort.MAX_CHANNEL_GROUP_SPACING)
        self.sample_width_vcmd = (self.register(self.validate), '%d', '%P', DataPort.MIN_SAMPLE_WIDTH, DataPort.MAX_SAMPLE_WIDTH)
        self.sample_grouping_vcmd = (self.register(self.validate), '%d', '%P', DataPort.MIN_SAMPLE_GROUPING, DataPort.MAX_SAMPLE_GROUPING)
        self.interval_vcmd = (self.register(self.validate), '%d', '%P', DataPort.MIN_INTERVAL_INTEGER, DataPort.MAX_INTERVAL_INTEGER)
        self.numerator_vcmd = (self.register(self.validate), '%d', '%P', DataPort.MIN_INTERVAL_NUMERATOR, DataPort.MAX_INTERVAL_NUMERATOR)
        self.offset_vcmd = (self.register(self.validate), '%d', '%P', DataPort.MIN_OFFSET, DataPort.MAX_OFFSET)
        self.column_vcmd = (self.register(self.validate), '%d', '%P', 0, Interface.MAX_COLUMNS - 1)

        dp_entry_box_validate_functions = [self.channels_vcmd,
                                           self.channel_grouping_vcmd,
                                           self.channel_group_spacing_vcmd,
                                           self.sample_width_vcmd,
                                           self.sample_grouping_vcmd,
                                           self.interval_vcmd,
                                           self.numerator_vcmd,
                                           self.offset_vcmd,
                                           self.column_vcmd,
                                           self.column_vcmd]

        # Used to validate interface parameter entry widget values
        self.columns_vcmd = (self.register(self.validate), '%d', '%P', Interface.MIN_COLUMNS, Interface.MAX_COLUMNS)
        self.columns_per_row_vcmd = (self.register(self.validate), '%d', '%P', 1, Interface.MAX_COLUMNS_PER_ROW)
        self.rows_vcmd = (self.register(self.validate), '%d', '%P', self.MIN_ROWS_IN_FRAME, self.MAX_ROWS_IN_FRAME)
        self.denominator_vcmd = (
            self.register(self.validate), '%d', '%P', Interface.MIN_INTERVAL_DENOMINATOR, Interface.MAX_INTERVAL_DENOMINATOR)
        self.bulk_hstart_vcmd = (self.register(self.validate), '%d', '%P', Interface.MIN_BULK_HORIZONTAL_START,
                                 Interface.MAX_BULK_HORIZONTAL_START)
        self.bulk_width_vcmd = (self.register(self.validate_values), '%d', '%P', [0, 2, 5, 10])
        self.handover_width_vcmd = (self.register(self.validate), '%d', '%P', Interface.MIN_TAIL_WIDTH, Interface.MAX_TAIL_WIDTH)
        self.tail_width_vcmd = (self.register(self.validate), '%d', '%P', Interface.MIN_TAIL_WIDTH, Interface.MAX_TAIL_WIDTH)
        self.s0_width_vcmd = (self.register(self.validate), '%d', '%P', Interface.MIN_S0_WIDTH, Interface.MAX_S0_WIDTH)
        self.row_rate_vcmd = (self.register(self.validate), '%d', '%P', Interface.MIN_ROW_RATE, Interface.MAX_ROW_RATE)

        master.bind('<Control-h>', self.toggle_ui)

        frame = tk.Frame(master, width=window_width, height=data_frame_height, bd=1, relief=tk.FLAT)
        frame.tk_setPalette(background=self.PREFERRED_GRAY)
        frame.pack(fill=tk.Y, expand=tk.YES)

        # Our data port parameter entry boxes go inside config_frame
        self.config_frame = tk.Frame(frame, bd=1, relief=tk.FLAT)
        self.config_frame.tk_setPalette(background=self.PREFERRED_GRAY)
        self.config_frame.pack(expand=tk.NO)

        self.s0s1_enabled_tk = tk.BooleanVar(value=self.interface.s0s1_enabled)
        self.s0_ta_enable_tk = tk.BooleanVar(value=self.interface.s0_handover_enabled)
        self.cd0_enable_tk = tk.BooleanVar(value=self.interface.cds_guard_enabled)
        self.bulk_guard_enabled_tk = tk.BooleanVar(value=self.interface.bulk_guard_enabled)

        # We'll use these to detect bus clashes
        self.bit_slots_source = []
        self.bit_slots_source_clashed = []
        self.bit_slots_sink = []
        self.bit_slots_sink_clashed = []
        self.bit_slots_turnaround = []

        self.dp_enable_check_button_vars = []
        self.dp_direction_check_button_vars = []
        self.dp_ta_enable_check_button_vars = []
        self.dp_tail_enable_check_button_vars = []
        self.dp_zero_enable_check_button_vars = []
        self.dp_entry_boxes = []
        self.dp_name_entry_boxes = []
        self.dp_parameter_labels = []

        # Data port name entry widgets
        for count, color in enumerate(self.DP_COLORS):
            self.dp_name_entry_boxes.append(
                tk.Entry(self.config_frame, width=self.NUMBER_ENTRY_WIDTH, bg=color, justify=tk.CENTER, relief=tk.FLAT,
                         font="TkDefaultFont " + str(
                             self.TEXT_SIZE), highlightcolor=self.PREFERRED_GRAY, highlightthickness=1))
            self.dp_name_entry_boxes[count].grid(row=0, column=count+1)

        # Data port parameter label widgets
        count = 0
        for count, title in enumerate(self.DP_PARAMETER_DESCRIPTIONS):
            self.dp_parameter_labels.append(tk.Label(self.config_frame, text=title, anchor=tk.E, font=(self.APP_FONT, self.TEXT_SIZE)))
            self.dp_parameter_labels[count].grid(row=count+1, column=0)
        # Shift down sample rate label
        self.dp_parameter_labels[-1].grid(row=count+2, column=0)

        # Data port entry widgets
        for entry_row in range(0, DataPort.NUM_DP_PARAMETERS):  # Columns
            for entry_column, data_port in enumerate(self.interface.data_ports):  # Rows
                self.dp_entry_boxes.append(self.create_entry(dp_entry_box_validate_functions[entry_row]))
                self.dp_entry_boxes[-1].grid(row=entry_row + 1, column=entry_column + 1, padx=3)
                self.dp_entry_boxes[-1].bind('<Return>', self.master_focus)

        # Data port direction checkbutton widgets
        self.dp_sample_rate_labels = []
        for count, data_port in enumerate(self.interface.data_ports):
            self.dp_direction_check_button_vars.append(tk.BooleanVar(value=data_port.source))
            cb = tk.Checkbutton(self.config_frame, justify=tk.CENTER, variable=self.dp_direction_check_button_vars[count])
            cb.grid(row=DataPort.NUM_DP_PARAMETERS+1, column=count+1)

            self.dp_ta_enable_check_button_vars.append(tk.BooleanVar(value=data_port.handover))
            cb = tk.Checkbutton(self.config_frame, justify=tk.CENTER, variable=self.dp_ta_enable_check_button_vars[count])
            cb.grid(row=DataPort.NUM_DP_PARAMETERS+2, column=count+1)

            self.dp_tail_enable_check_button_vars.append(tk.BooleanVar(value=data_port.tail))
            cb = tk.Checkbutton(self.config_frame, justify=tk.CENTER, variable=self.dp_tail_enable_check_button_vars[count])
            cb.grid(row=DataPort.NUM_DP_PARAMETERS+3, column=count+1)

            self.dp_zero_enable_check_button_vars.append(tk.BooleanVar(value=data_port.guard))
            cb = tk.Checkbutton(self.config_frame, justify=tk.CENTER, variable=self.dp_zero_enable_check_button_vars[count])
            cb.grid(row=DataPort.NUM_DP_PARAMETERS+4, column=count+1)

            self.dp_enable_check_button_vars.append(tk.BooleanVar(value=data_port.enabled))
            cb = tk.Checkbutton(self.config_frame, justify=tk.CENTER, variable=self.dp_enable_check_button_vars[count])
            cb.grid(row=DataPort.NUM_DP_PARAMETERS+5, column=count+1)

            self.dp_sample_rate_labels.append(
                tk.Label(self.config_frame, text=data_port.sample_rate, anchor=tk.CENTER, font=(self.APP_FONT, self.TEXT_SIZE)))
            self.dp_sample_rate_labels[count].grid(row=DataPort.NUM_DP_PARAMETERS+7, column=count+1)

        # Interface parameter label widgets
        self.frame_labels = [
            tk.Label(self.config_frame, text='Data Port Parameters', anchor=tk.CENTER, font=(self.APP_FONT, self.TEXT_SIZE+6), padx=10)]
        self.frame_labels[-1].grid(row=0, column=0)

        # Interface Parameters
        self.frame_labels.append(
            tk.Label(self.config_frame, text='Interface Parameters', anchor=tk.CENTER, font=(self.APP_FONT, self.TEXT_SIZE+6), padx=10))
        self.frame_labels[-1].grid(row=0, column=Interface.NUM_DATA_PORTS+2, columnspan=2)
        for count, text in enumerate(self.INTERFACE_PARAMETER_TITLES):
            self.frame_labels.append(
                tk.Label(self.config_frame, text=self.INTERFACE_PARAMETER_TITLES[count], anchor=tk.CENTER,
                         font=(self.APP_FONT, self.TEXT_SIZE),
                         padx=10))
            self.frame_labels[-1].grid(row=count+1, column=Interface.NUM_DATA_PORTS+2)

        # SSP Label
        self.frame_labels.append(
            tk.Label(self.config_frame, text='Calculated SSP Row Interval', anchor=tk.CENTER, font=(self.APP_FONT, self.TEXT_SIZE),
                     padx=10))
        self.frame_labels[-1].grid(row=17, column=Interface.NUM_DATA_PORTS+2)
        self.frame_labels.append(
            tk.Label(self.config_frame, text=self.interface.interval_lcm, anchor=tk.CENTER, font=(self.APP_FONT, self.TEXT_SIZE), padx=10))
        self.frame_labels[-1].grid(row=17, column=Interface.NUM_DATA_PORTS+3)

        # Interface parameter entry widgets

        # Rows to draw
        self.rpf_entry = self.create_entry(self.rows_vcmd)
        self.rpf_entry.grid(row=1, column=Interface.NUM_DATA_PORTS+3)
        self.rpf_entry.bind('<Return>', self.master_focus)

        # Columns per row
        self.cpr_entry = self.create_entry(self.columns_per_row_vcmd)
        self.cpr_entry.grid(row=2, column=Interface.NUM_DATA_PORTS+3)
        self.cpr_entry.bind('<Return>', self.master_focus)

        # S0/S1 enable
        cb = tk.Checkbutton(self.config_frame, justify=tk.CENTER, variable=self.s0s1_enabled_tk)
        cb.grid(row=3, column=Interface.NUM_DATA_PORTS+3)

        # S0 width
        self.s0w_entry = self.create_entry(self.s0_width_vcmd)
        self.s0w_entry.grid(row=4, column=Interface.NUM_DATA_PORTS+3)
        self.s0w_entry.bind('<Return>', self.master_focus)

        # S0 handover enable
        cb = tk.Checkbutton(self.config_frame, justify=tk.CENTER, variable=self.s0_ta_enable_tk)
        cb.grid(row=5, column=Interface.NUM_DATA_PORTS+3)

        # Control Data Stream guard
        cb = tk.Checkbutton(self.config_frame, justify=tk.CENTER, variable=self.cd0_enable_tk)
        cb.grid(row=6, column=Interface.NUM_DATA_PORTS+3)

        # Handover width
        self.handover_width_entry = self.create_entry(self.handover_width_vcmd)
        self.handover_width_entry.grid(row=7, column=Interface.NUM_DATA_PORTS+3)
        self.handover_width_entry.bind('<Return>', self.master_focus)

        # Tail width
        self.tail_width_entry = self.create_entry(self.tail_width_vcmd)
        self.tail_width_entry.grid(row=8, column=Interface.NUM_DATA_PORTS+3)
        self.tail_width_entry.bind('<Return>', self.master_focus)

        # Fractional interval denominator
        self.fid_entry = self.create_entry(self.denominator_vcmd)
        self.fid_entry.grid(row=9, column=Interface.NUM_DATA_PORTS+3)
        self.fid_entry.bind('<Return>', self.master_focus)

        # Bulk horizontal start
        self.bulk_hstart_entry = self.create_entry(self.bulk_hstart_vcmd)
        self.bulk_hstart_entry.grid(row=10, column=Interface.NUM_DATA_PORTS+3)
        self.bulk_hstart_entry.bind('<Return>', self.master_focus)

        # Bulk width
        self.bulk_width_entry = self.create_entry(self.bulk_width_vcmd)
        self.bulk_width_entry.grid(row=11, column=Interface.NUM_DATA_PORTS+3)
        self.bulk_width_entry.bind('<Return>', self.master_focus)

        # Bulk guard enable
        cb = tk.Checkbutton(self.config_frame, justify=tk.CENTER, variable=self.bulk_guard_enabled_tk)
        cb.grid(row=12, column=Interface.NUM_DATA_PORTS+3)

        # Row rate
        self.row_rate_entry = self.create_entry(self.row_rate_vcmd)
        self.row_rate_entry.grid(row=13, column=Interface.NUM_DATA_PORTS + 3)
        self.row_rate_entry.bind('<Return>', self.master_focus)

        # render_frame contains the canvas widget where we draw the SoundWire frame
        render_frame = tk.Frame(frame)
        render_frame.tk_setPalette(background=self.PREFERRED_GRAY)
        render_frame.pack(side=tk.BOTTOM, expand=1, fill=tk.Y)

        # Will hold frozen column headers
        self.header_canvas = tk.Canvas(render_frame, relief=tk.FLAT, bd=0, highlightthickness=0, bg=self.PREFERRED_GRAY,
                                       width=self.canvas_width, height=self.ROW_SIZE)
        self.header_canvas.pack(expand=tk.FALSE, side=tk.TOP, fill=tk.Y)

        self.render_canvas = tk.Canvas(render_frame, relief=tk.FLAT, bd=0, highlightthickness=0, bg=self.PREFERRED_GRAY,
                                       width=self.canvas_width, height=canvas_height)
        self.render_canvas.pack(expand=tk.FALSE, side=tk.LEFT, fill=tk.Y)

        vbar = tk.Scrollbar(render_frame, relief=tk.FLAT, orient=tk.VERTICAL)
        self.render_canvas.config(yscrollcommand=vbar.set)

        vbar.config(command=self.render_canvas.yview)
        vbar.pack(side=tk.RIGHT, fill=tk.Y)

        # This is what enables scrolling with the mouse:
        self.render_canvas.bind("<ButtonPress-1>", self.scroll_start)
        self.render_canvas.bind("<B1-Motion>", self.scroll_move)
        if platform.system() == 'Windows':
            self.render_canvas.bind_all("<MouseWheel>", self._on_mousewheel_windows)
        else:
            self.render_canvas.bind_all("<MouseWheel>", self._on_mousewheel_mac)

        # Read configuration file button
        cmd2 = self.load_csv_file
        btn2 = tk.Button(self.config_frame, text='Load Configuration', default='active', command=cmd2,
                         font=(self.APP_FONT, self.TEXT_SIZE + 3), relief=tk.FLAT)
        btn2.grid(column=Interface.NUM_DATA_PORTS + 2, columnspan=2, row=DataPort.NUM_DP_PARAMETERS + 4,
                  sticky=tk.N + tk.S + tk.E + tk.W, padx=10, pady=3)

        # Write configuration file button
        cmd3 = self.save_csv_file
        btn3 = tk.Button(self.config_frame, text='Save Configuration', default='active', command=cmd3,
                         font=(self.APP_FONT, self.TEXT_SIZE + 3), relief=tk.FLAT)
        btn3.grid(column=Interface.NUM_DATA_PORTS + 2, columnspan=2, row=DataPort.NUM_DP_PARAMETERS + 5,
                  sticky=tk.N + tk.S + tk.E + tk.W, padx=10, pady=3)

        # Redraw data ports button
        cmd1 = self.refresh_data_ports
        btn1 = tk.Button(self.config_frame, text='Redraw', default='active', command=cmd1,
                         font=(self.APP_FONT, self.TEXT_SIZE + 3), relief=tk.FLAT)
        btn1.grid(column=Interface.NUM_DATA_PORTS + 2, columnspan=2, row=DataPort.NUM_DP_PARAMETERS + 6,
                  sticky=tk.N + tk.S + tk.E + tk.W, padx=10, pady=3)

        self.update_ui()
        self.refresh_data_ports()

    # Validates entry widget text
    @staticmethod
    def validate(action, value_if_allowed, low, high):
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

    @staticmethod
    def validate_values(action, value_if_allowed, values):
        if action == '1':  # Entry action
            try:
                if value_if_allowed in values:
                    return True
                else:
                    return False
            except ValueError:
                return False
        else:
            return True

    def scroll_start(self, event):
        self.render_canvas.scan_mark(event.x, event.y)

    def scroll_move(self, event):
        self.render_canvas.scan_dragto(event.x, event.y, gain=2)

    def _on_mousewheel_mac(self, event):
        self.render_canvas.yview_scroll(-1 * event.delta, 'units')

    def _on_mousewheel_windows(self, event):
        self.render_canvas.yview_scroll(-1 * (event.delta / 120), 'units')

    # Hides the configuration entry frame
    def toggle_ui(self, *_args):
        try:
            self.config_frame.pack_info()
        except tk.TclError:
            self.config_frame.pack()
        else:
            self.config_frame.forget()

    # Extends int() to return 0 when passed an empty string from an entry widget
    @staticmethod
    def st_int(str_in):
        if not isinstance(str_in, str):
            raise TypeError('Expected str for str_in')
        if str_in == '':
            return 0
        else:
            return int(str_in)

    def master_focus(self, *_args):
        self.master.focus()

    # Writes data port and frame configuration into to file
    def save_csv_file(self):

        self.refresh_data_ports()

        # Write csv file
        filename = filedialog.asksaveasfilename(initialdir=".", title="Select an output parameter file name", defaultextension=".csv",
                                                filetypes=[("CSV Files", "*.csv")])

        if filename:
            with io.open(filename, 'w', encoding='utf8') as outfile:
                writer = csv.writer(outfile, delimiter=',', lineterminator='\n')
                frame_values = [self.rows_in_frame, self.interface.columns_per_row, self.interface.s0s1_enabled,
                                self.interface.s0_width, self.interface.s0_handover_enabled, self.interface.cds_guard_enabled,
                                self.interface.handover_width, self.interface.tail_width, self.interface.interval_denominator,
                                self.interface.bulk_horizontal_start, self.interface.bulk_width,
                                self.interface.bulk_guard_enabled, self.interface.row_rate]
                for count, value in enumerate(frame_values):
                    row = [self.INTERFACE_PARAMETER_TITLES[count]] + [str(value)]
                    writer.writerow(row)
                row = ['Data Port Names'] + [data_port.name for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = ['Data Port Channels'] + [str(data_port.channels) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = ['Data Port Channel Grouping'] + [str(data_port.channel_grouping) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = ['Data Port Channel Group Spacing'] + [str(data_port.channel_group_spacing) for data_port in
                                                             self.interface.data_ports]
                writer.writerow(row)
                row = ['Data Port Sample Width'] + [str(data_port.sample_width) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = ['Data Port Sample Grouping'] + [str(data_port.sample_grouping) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = ['Data Port Interval Integer'] + [str(data_port.interval_integer) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = ['Data Port Interval Numerator'] + [str(data_port.interval_numerator) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = ['Data Port Offset'] + [str(data_port.offset) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = ['Data Port Horizontal Start'] + [str(data_port.h_start) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = ['Data Port Horizontal Stop'] + [str(data_port.h_stop) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = ['Source'] + [str(data_port.source) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = ['Data Port Handover Enabled'] + [str(data_port.handover) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = ['Data Port Tail Enabled'] + [str(data_port.tail) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = ['Data Port Guard Enabled'] + [str(data_port.guard) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = ['Data Port Enabled'] + [str(data_port.enabled) for data_port in self.interface.data_ports]
                writer.writerow(row)
            outfile.close()

            # Write SVG export file if the canvasvg module was found
            if canvasvg:
                canvasvg.saveall(os.path.splitext(filename)[0] + '.svg', self.render_canvas)

    # Writes UI elements
    def update_ui(self):

        # Update data port names
        for count, x in enumerate(self.dp_name_entry_boxes):
            x.delete(0, tk.END)
            x.insert(0, self.interface.data_ports[count].name)

        # Data port entry widgets
        for entry_column, data_port in enumerate(self.interface.data_ports):  # Rows
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 0 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 0 + entry_column].insert(0,
                                                                                         self.interface.data_ports[entry_column].channels)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 1 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 1 + entry_column].insert(0, self.interface.data_ports[
                entry_column].channel_grouping)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 2 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 2 + entry_column].insert(0,
                                                                                         self.interface.data_ports[
                                                                                             entry_column].channel_group_spacing)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 3 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 3 + entry_column].insert(0,
                                                                                         self.interface.data_ports[
                                                                                             entry_column].sample_width)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 4 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 4 + entry_column].insert(0,
                                                                                         self.interface.data_ports[
                                                                                             entry_column].sample_grouping)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 5 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 5 + entry_column].insert(0,
                                                                                         self.interface.data_ports[
                                                                                             entry_column].interval_integer)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 6 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 6 + entry_column].insert(0,
                                                                                         self.interface.data_ports[
                                                                                             entry_column].interval_numerator)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 7 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 7 + entry_column].insert(0,
                                                                                         self.interface.data_ports[entry_column].offset)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 8 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 8 + entry_column].insert(0,
                                                                                         self.interface.data_ports[entry_column].h_start)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 9 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 9 + entry_column].insert(0,
                                                                                         self.interface.data_ports[entry_column].h_stop)

        # Data port direction
        for count, x in enumerate(self.dp_direction_check_button_vars):
            x.set(self.interface.data_ports[count].source)

        # Data port turn around enables
        for count, x in enumerate(self.dp_ta_enable_check_button_vars):
            x.set(self.interface.data_ports[count].handover)

        # Data port tail enables
        for count, x in enumerate(self.dp_tail_enable_check_button_vars):
            x.set(self.interface.data_ports[count].tail)

        # Data port zero enables
        for count, x in enumerate(self.dp_zero_enable_check_button_vars):
            x.set(self.interface.data_ports[count].guard)

        # Data port enables
        for count, x in enumerate(self.dp_enable_check_button_vars):
            x.set(self.interface.data_ports[count].enabled)

        # Rows per frame
        self.rpf_entry.delete(0, tk.END)
        self.rpf_entry.insert(tk.END, self.rows_in_frame)
        # Columns per row
        self.cpr_entry.delete(0, tk.END)
        self.cpr_entry.insert(tk.END, self.interface.columns_per_row)
        # S0/S1 enable
        self.s0s1_enabled_tk.set(self.interface.s0s1_enabled)
        # S0 width
        self.s0w_entry.delete(0, tk.END)
        self.s0w_entry.insert(tk.END, self.interface.s0_width)
        # S0 TA enable
        self.s0_ta_enable_tk.set(self.interface.s0_handover_enabled)
        # CDS guard enable
        self.cd0_enable_tk.set(self.interface.cds_guard_enabled)
        # Handover width
        self.handover_width_entry.delete(0, tk.END)
        self.handover_width_entry.insert(tk.END, self.interface.handover_width)
        # Tail width
        self.tail_width_entry.delete(0, tk.END)
        self.tail_width_entry.insert(tk.END, self.interface.tail_width)
        # Fractional interval denominator
        self.fid_entry.delete(0, tk.END)
        self.fid_entry.insert(tk.END, self.interface.interval_denominator)
        # Bulk channel horizontal start
        self.bulk_hstart_entry.delete(0, tk.END)
        self.bulk_hstart_entry.insert(tk.END, self.interface.bulk_horizontal_start)
        # Bulk channel width
        self.bulk_width_entry.delete(0, tk.END)
        self.bulk_width_entry.insert(tk.END, self.interface.bulk_width)
        # Bulk channel guard enable
        self.bulk_guard_enabled_tk.set(self.interface.bulk_guard_enabled)
        # Row rate
        self.row_rate_entry.delete(0, tk.END)
        self.row_rate_entry.insert(tk.END, self.interface.row_rate)

        # Update sample rate labels
        for count, x in enumerate(self.dp_sample_rate_labels):
            if self.interface.data_ports[count].interval_integer != 0:
                sample_rate = "{:.2f}".format(self.interface.data_ports[count].sample_grouping * self.interface.row_rate / (
                        self.interface.data_ports[count].interval_integer + self.interface.data_ports[count].interval_numerator /
                        self.interface.interval_denominator))
                x.config(text=sample_rate)
            else:
                x.config(text='Error')

        # Update Interval LCM
        self.frame_labels[-1].config(text=self.interface.interval_lcm)

    # Reads UI elements
    def update_model(self):

        # Update data port names
        for count, entry_box in enumerate(self.dp_name_entry_boxes):
            self.interface.data_ports[count].name = entry_box.get()

        # Data port entry widgets
        for entry_column, data_port in enumerate(self.interface.data_ports):  # Rows
            self.interface.data_ports[entry_column].channels = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 0 + entry_column].get())

            self.interface.data_ports[entry_column].channel_grouping = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 1 + entry_column].get())

            self.interface.data_ports[entry_column].channel_group_spacing = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 2 + entry_column].get())

            self.interface.data_ports[entry_column].sample_width = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 3 + entry_column].get())

            self.interface.data_ports[entry_column].sample_grouping = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 4 + entry_column].get())

            self.interface.data_ports[entry_column].interval_integer = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 5 + entry_column].get())

            self.interface.data_ports[entry_column].interval_numerator = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 6 + entry_column].get())

            self.interface.data_ports[entry_column].offset = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 7 + entry_column].get())

            self.interface.data_ports[entry_column].h_start = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 8 + entry_column].get())

            self.interface.data_ports[entry_column].h_stop = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 9 + entry_column].get())

        # read & error check rows per frame
        self.rows_in_frame = max(self.MIN_ROWS_IN_FRAME, min(self.MAX_ROWS_IN_FRAME, self.st_int(self.rpf_entry.get())))

        # read & error check columns per row
        self.interface.columns_per_row = max(Interface.MIN_COLUMNS_PER_ROW, min(Interface.MAX_COLUMNS_PER_ROW, self.st_int(self.cpr_entry.get())))
        # If odd, make even
        self.interface.columns_per_row -= self.interface.columns_per_row % 2

        self.interface.s0s1_enabled = self.s0s1_enabled_tk.get()

        # read & error check s0 width
        self.interface.s0_width = max(Interface.MIN_S0_WIDTH, min(Interface.MAX_S0_WIDTH, self.st_int(self.s0w_entry.get())))

        self.interface.s0_handover_enabled = self.s0_ta_enable_tk.get()

        self.interface.cds_guard_enabled = self.cd0_enable_tk.get()

        # read & error check Handover width
        self.interface.handover_width = max(Interface.MIN_HANDOVER_WIDTH,
                                            min(Interface.MAX_HANDOVER_WIDTH, self.st_int(self.handover_width_entry.get())))

        # read & error check Tail width
        self.interface.tail_width = max(Interface.MIN_TAIL_WIDTH, min(Interface.MAX_TAIL_WIDTH, self.st_int(self.tail_width_entry.get())))

        # read & error check fractional interval denominator
        self.interface.interval_denominator = max(Interface.MIN_INTERVAL_DENOMINATOR,
                                                  min(Interface.MAX_INTERVAL_DENOMINATOR, self.st_int(self.fid_entry.get())))

        # read & error check bulk hstart
        self.interface.bulk_horizontal_start = max(Interface.MIN_COLUMNS, min(Interface.MAX_COLUMNS, self.st_int(
            self.bulk_hstart_entry.get())))

        # read & error check bulk width
        if self.st_int(self.bulk_width_entry.get()) not in [0, 2, 5, 10]:
            self.interface.bulk_width = 0
        else:
            self.interface.bulk_width = self.st_int(self.bulk_width_entry.get())

        self.interface.bulk_guard_enabled = self.bulk_guard_enabled_tk.get()

        # read & error check row rate
        # self.interface.row_rate = max(1, min(self.interface.row_rate - 1, self.st_int(self.interface.row_rate_entry.get())))
        self.interface.row_rate = self.st_int(self.row_rate_entry.get())

        # Data port direction check button widgets
        for count, direction in enumerate(self.dp_direction_check_button_vars):
            self.interface.data_ports[count].source = bool(direction.get())

        # Data port handover enable check button widgets
        for count, x in enumerate(self.dp_ta_enable_check_button_vars):
            self.interface.data_ports[count].handover = bool(x.get())

        # Data port tail enable check button widgets
        for count, x in enumerate(self.dp_tail_enable_check_button_vars):
            self.interface.data_ports[count].tail = bool(x.get())

        # Data port guard enable check button widgets
        for count, x in enumerate(self.dp_zero_enable_check_button_vars):
            self.interface.data_ports[count].guard = bool(x.get())

        # Data port enable check button widgets
        for count, x in enumerate(self.dp_enable_check_button_vars):
            self.interface.data_ports[count].enabled = bool(x.get())

    # Draws all data ports
    def refresh_data_ports(self):
        self.master_focus()
        # Clear the canvas
        self.render_canvas.delete(tk.ALL)
        self.header_canvas.delete(tk.ALL)

        self.bit_slots_source_clashed[:] = []
        self.bit_slots_source[:] = []

        self.bit_slots_sink_clashed[:] = []
        self.bit_slots_sink[:] = []

        self.bit_slots_turnaround[:] = []

        self.update_model()
        self.update_ui()

        self.render_canvas.config(scrollregion=(0, 2 * self.ROW_SIZE + 2, self.canvas_width, (self.rows_in_frame + 2.5) * self.ROW_SIZE),
                                  bg=self.PREFERRED_GRAY)

        self.render_canvas.create_rectangle(0, 0, self.canvas_width, (self.rows_in_frame + 2) * self.ROW_SIZE, width=0,
                                            fill=self.PREFERRED_GRAY)

        self.render_canvas.create_rectangle(self.COLUMN_SIZE * 1.5, self.ROW_SIZE * 2,
                                            (self.interface.columns_per_row + 1.5) * self.COLUMN_SIZE,
                                            (self.rows_in_frame + 2) * self.ROW_SIZE, outline=self.DARK_GRAY, width=3)

        self.header_canvas.create_line(self.COLUMN_SIZE * 1.5 - self.AUX_CANVAS_FUDGE - 1, self.ROW_SIZE - 2,
                                       (self.interface.columns_per_row + 1.5) * self.COLUMN_SIZE - self.AUX_CANVAS_FUDGE + 1,
                                       self.ROW_SIZE - 2, fill=self.DARK_GRAY, width=3)

        # Draw data port color key to the hidden part of C2 so it appears in the exported SVG
        self.render_canvas.create_text(0.5 * self.COLUMN_SIZE, 0.5 * self.ROW_SIZE, text='Data Port Color Key:', anchor=tk.W,
                                       font=(self.APP_FONT,
                                             self.TEXT_SIZE + 2))
        for count, data_port in enumerate(self.interface.data_ports):
            self.render_canvas.create_rectangle((count + 3) * 2 * self.COLUMN_SIZE,
                                                0 * self.ROW_SIZE,
                                                (count + 4) * 2 * self.COLUMN_SIZE,
                                                1 * self.ROW_SIZE,
                                                fill=self.DP_COLORS[count], width=1)
            self.render_canvas.create_text((count + 3.75) * 2 * self.COLUMN_SIZE, 0.5 * self.ROW_SIZE - 2, text=data_port.name, anchor=tk.E,
                                           font=(self.APP_FONT, self.TEXT_SIZE + 2))

        # Column headers
        for column in range(0, self.interface.columns_per_row):
            self.render_canvas.create_text((column + 2) * self.COLUMN_SIZE, self.ROW_SIZE * 1.5, text=column,
                                           font=(self.APP_FONT, self.TEXT_SIZE))
            self.header_canvas.create_text((column + 2) * self.COLUMN_SIZE - self.AUX_CANVAS_FUDGE, self.ROW_SIZE * 0.5, text=column,
                                           font=(self.APP_FONT, self.TEXT_SIZE))

        # Row headers
        for row in range(0, self.rows_in_frame):
            self.render_canvas.create_text(1.25 * self.COLUMN_SIZE, (row + 2.5) * self.ROW_SIZE, text=row, anchor=tk.E,
                                           font=(self.APP_FONT, self.TEXT_SIZE))

        # Row Lines
        for count in range(1, self.rows_in_frame + 1):
            self.render_canvas.create_line(self.COLUMN_SIZE * 1.5 + 1, self.ROW_SIZE * (count + 1),
                                           (self.interface.columns_per_row + 1.5) * self.COLUMN_SIZE - 1,
                                           self.ROW_SIZE * (count + 1), fill=self.DARK_GRAY, width=3)
            # Bit slot key
            self.render_canvas.create_text((self.interface.columns_per_row + 2) * self.COLUMN_SIZE + 4, (count + 1.2) * self.ROW_SIZE,
                                           font=(self.APP_FONT, self.TEXT_SIZE - 2),
                                           justify=tk.CENTER, text='Source')
            self.render_canvas.create_line((self.interface.columns_per_row + 1.5) * self.COLUMN_SIZE + 4, self.ROW_SIZE * (count + 1.5) - 1,
                                           (self.interface.columns_per_row + 2.5) * self.COLUMN_SIZE - 1,
                                           self.ROW_SIZE * (count + 1.5) - 1, fill=self.LIGHT_GRAY)
            self.render_canvas.create_text((self.interface.columns_per_row + 2) * self.COLUMN_SIZE + 4, (count + 1.7) * self.ROW_SIZE,
                                           font=(self.APP_FONT, self.TEXT_SIZE - 2),
                                           justify=tk.CENTER, text='Sink')

        # S0 S1 Columns
        if self.interface.s0s1_enabled:
            if self.interface.columns_per_row > self.interface.s0_width + 3 + int(self.interface.cds_guard_enabled) + int(
                    self.interface.s0_handover_enabled) * self.interface.handover_width:
                # S0 Column(s)
                if self.interface.s0_handover_enabled:
                    for column_offset in range(0, self.interface.handover_width):
                        self.draw_column(self.interface.columns_per_row - column_offset - self.interface.s0_width - 1, 'TA')

                for column_offset in range(0, self.interface.s0_width):
                    self.draw_column(self.interface.columns_per_row - column_offset - 1, 'S0')

                self.draw_column(0, 'S1')

                for column_offset in range(0, self.interface.handover_width):
                    self.draw_column(1 + column_offset + self.interface.tail_width, 'TA')

                self.draw_column(1 + self.interface.handover_width + self.interface.tail_width, 'CDS')

                if self.interface.cds_guard_enabled:
                    self.draw_column(2 + self.interface.handover_width + self.interface.tail_width, 'GRD')

                for column_offset in range(0, self.interface.handover_width):
                    self.draw_column(2 + self.interface.handover_width + 2 * self.interface.tail_width + int(
                        self.interface.cds_guard_enabled) + column_offset, 'TA')

                for column_offset in range(0, self.interface.tail_width):
                    for count in range(0, self.rows_in_frame):
                        self.draw_tail(count, 1 + column_offset, self.PREFERRED_GRAY)
                        self.draw_tail(count, 2 + self.interface.tail_width + self.interface.handover_width + int(
                            self.interface.cds_guard_enabled) + column_offset,
                                       self.PREFERRED_GRAY)
            else:
                self.master.update()
                messagebox.showwarning('Error!', 'Unable to draw S0, S1 & control stream columns: Too few columns in each row.')
        else:
            self.draw_column(0, 'CDS')
            if self.interface.cds_guard_enabled:
                self.draw_column(1, 'GRD')
            for column_offset in range(0, self.interface.handover_width):
                self.draw_column(int(self.interface.cds_guard_enabled) + column_offset + 1, 'TA')
                self.draw_column(self.interface.columns_per_row - column_offset - 1, 'TA')

        # Draw the bulk channel
        if self.interface.bulk_width:
            for count in range(0, self.interface.bulk_width):
                self.draw_column(self.interface.bulk_horizontal_start + count, 'BULK')
            if self.interface.bulk_guard_enabled:
                self.draw_column(self.interface.bulk_horizontal_start + self.interface.bulk_width, 'GRD')
            for column_offset in range(0, self.interface.tail_width):
                for count in range(0, self.rows_in_frame):
                    self.draw_tail(count,
                                   column_offset + self.interface.bulk_horizontal_start + self.interface.bulk_width +
                                   self.interface.bulk_guard_enabled, self.PREFERRED_GRAY)
            for column_offset in range(0, self.interface.handover_width):
                self.draw_column(self.interface.bulk_horizontal_start - column_offset - 1, 'TA')
                self.draw_column(
                    self.interface.bulk_horizontal_start + self.interface.bulk_width + column_offset + self.interface.tail_width +
                    self.interface.bulk_guard_enabled, 'TA')

        error_text = ''
        for count, data_port in enumerate(self.interface.data_ports):
            if data_port.enabled:
                temp_text = self.draw_data_port(self.interface.data_ports[count], self.DP_COLORS[count])
                if len(temp_text):
                    error_text += temp_text + 'In ' + self.interface.data_ports[count].name + '\n'

        self.master.update()

        if len(error_text):
            messagebox.showwarning("Warning", "Some data ports could not be drawn since: " + error_text)

        if len(self.bit_slots_source_clashed):
            messagebox.showwarning("Warning", 'Bus clash detected in ' + str(
                len(self.bit_slots_source_clashed)) + ' bit slots. \nThey are colored black.')

        if len(self.bit_slots_sink_clashed):
            messagebox.showwarning("Warning", 'Read overlap detected in ' + str(
                len(self.bit_slots_sink_clashed)) + ' bit slots. \nThey are colored red.')

    # Reads data port and frame configuration from a file
    def load_csv_file(self):
        self.master_focus()

        filename = filedialog.askopenfilename(initialdir=".", title="Select an input parameter filename",
                                              filetypes=[("CSV Files", "*.csv")])

        if filename:
            with open(filename) as data_file:
                csv_data = csv.reader(data_file)
                for count, row in enumerate(csv_data):
                    if count == 0:
                        self.rows_in_frame = self.st_int(row[1])
                    elif count == 1:
                        self.interface.columns_per_row = self.st_int(row[1])
                    elif count == 2:
                        self.interface.s0s1_enabled = bool(distutils.util.strtobool(row[1]))
                    elif count == 3:
                        self.interface.s0_width = self.st_int(row[1])
                    elif count == 4:
                        self.interface.s0_handover_enabled = bool(distutils.util.strtobool(row[1]))
                    elif count == 5:
                        self.interface.cds_guard_enabled = bool(distutils.util.strtobool(row[1]))
                    elif count == 6:
                        self.interface.handover_width = self.st_int(row[1])
                    elif count == 7:
                        self.interface.tail_width = self.st_int(row[1])
                    elif count == 8:
                        self.interface.interval_denominator = self.st_int(row[1])
                    elif count == 9:
                        self.interface.bulk_horizontal_start = self.st_int(row[1])
                    elif count == 10:
                        self.interface.bulk_width = self.st_int(row[1])
                    elif count == 11:
                        self.interface.bulk_guard_enabled = bool(distutils.util.strtobool(row[1]))
                    elif count == 12:
                        self.interface.row_rate = self.st_int(row[1])
                    elif count == 13:
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.name = row[obj_count + 1]
                    elif count == 14:
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.channels = self.st_int(row[obj_count + 1])
                    elif count == 15:
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.channel_grouping = self.st_int(row[obj_count + 1])
                    elif count == 16:
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.channel_group_spacing = self.st_int(row[obj_count + 1])
                    elif count == 17:
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.sample_width = self.st_int(row[obj_count + 1])
                    elif count == 18:
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.sample_grouping = self.st_int(row[obj_count + 1])
                    elif count == 19:
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.interval_integer = self.st_int(row[obj_count + 1])
                    elif count == 20:
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.interval_numerator = self.st_int(row[obj_count + 1])
                    elif count == 21:
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.offset = self.st_int(row[obj_count + 1])
                    elif count == 22:
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.h_start = self.st_int(row[obj_count + 1])
                    elif count == 23:
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.h_stop = self.st_int(row[obj_count + 1])
                    elif count == 24:
                        if len(row[1:]) == Interface.NUM_DATA_PORTS:
                            for obj_count, data_port in enumerate(self.interface.data_ports):
                                data_port.source = bool(distutils.util.strtobool(row[obj_count + 1]))
                        else:
                            messagebox.showwarning("Warning", "Error reading CSV file.\n" + str(
                                Interface.NUM_DATA_PORTS) + ' data port entry columns expected in row ' + str(
                                count + 1) + '.\nFound ' + str(len(row[1:])) + '.')
                            return
                    elif count == 25:
                        if len(row[1:]) == Interface.NUM_DATA_PORTS:
                            for obj_count, data_port in enumerate(self.interface.data_ports):
                                data_port.handover = bool(distutils.util.strtobool(row[obj_count + 1]))
                        else:
                            messagebox.showwarning("Warning", "Error reading CSV file.\n" + str(
                                Interface.NUM_DATA_PORTS) + ' data port entry columns expected in row ' + str(
                                count + 1) + '.\nFound ' + str(len(row[1:])) + '.')
                            return
                    elif count == 26:
                        if len(row[1:]) == Interface.NUM_DATA_PORTS:
                            for obj_count, data_port in enumerate(self.interface.data_ports):
                                data_port.tail = bool(distutils.util.strtobool(row[obj_count + 1]))
                        else:
                            messagebox.showwarning("Warning", "Error reading CSV file.\n" + str(
                                Interface.NUM_DATA_PORTS) + ' data port entry columns expected in row ' + str(
                                count + 1) + '.\nFound ' + str(len(row[1:])) + '.')
                            return
                    elif count == 27:
                        if len(row[1:]) == Interface.NUM_DATA_PORTS:
                            for obj_count, data_port in enumerate(self.interface.data_ports):
                                data_port.guard = bool(distutils.util.strtobool(row[obj_count + 1]))
                        else:
                            messagebox.showwarning("Warning", "Error reading CSV file.\n" + str(
                                Interface.NUM_DATA_PORTS) + ' data port entry columns expected in row ' + str(
                                count + 1) + '.\nFound ' + str(len(row[1:])) + '.')
                            return
                    elif count == 28:
                        if len(row[1:]) == Interface.NUM_DATA_PORTS:
                            for obj_count, data_port in enumerate(self.interface.data_ports):
                                data_port.enabled = bool(distutils.util.strtobool(row[obj_count + 1]))
                        else:
                            messagebox.showwarning("Warning", "Error reading CSV file.\n" + str(
                                Interface.NUM_DATA_PORTS) + ' data port entry columns expected in row ' + str(
                                count + 1) + '.\nFound ' + str(len(row[1:])) + '.')
                            return

            data_file.close()
            self.master.update()
            self.update_ui()
            self.refresh_data_ports()

    # Draws a single repeating column in a frame
    def draw_column(self, column, text):
        if not isinstance(column, int):
            raise TypeError('Expected int for column')
        if not isinstance(text, str):
            raise TypeError('Expected str for text')
        for row in range(0, self.rows_in_frame):
            if text == 'TA':
                self.draw_handover(row, column)
            else:
                self.render_canvas.create_text((column + 2) * self.COLUMN_SIZE, (row + 2.5) * self.ROW_SIZE, text=text,
                                               font=(self.APP_FONT, self.TEXT_SIZE))
                self.check_bus_clash(row, column, 'write')

    # Draws a handover bit slot
    def draw_handover(self, row, column):
        if not isinstance(row, int):
            raise TypeError('Expected int for row')
        if not isinstance(column, int):
            raise TypeError('Expected int for column')
        self.render_canvas.create_line((column + 1.725) * self.COLUMN_SIZE, (row + 2.35) * self.ROW_SIZE,
                                       (column + 2.275) * self.COLUMN_SIZE,
                                       (row + 2.35) * self.ROW_SIZE, arrow=tk.LAST)
        self.render_canvas.create_line((column + 1.725) * self.COLUMN_SIZE, (row + 2.65) * self.ROW_SIZE,
                                       (column + 2.275) * self.COLUMN_SIZE,
                                       (row + 2.65) * self.ROW_SIZE, arrow=tk.FIRST)
        self.check_bus_clash(row, column, 'handover')

    # Draws a tail bit slot
    def draw_tail(self, row, column, color):
        if not isinstance(row, int):
            raise TypeError('Expected int for row')
        if not isinstance(column, int):
            raise TypeError('Expected int for column')
        if not isinstance(color, str):
            raise TypeError('Expected str for color')
        direction = 1
        self.render_canvas.create_rectangle((column + 1.5) * self.COLUMN_SIZE,
                                            (row + 2 + 0.5 * (direction == 0)) * self.ROW_SIZE + 2 * (
                                                    direction == 1) + 0 * (direction == 0),
                                            (column + 2.5) * self.COLUMN_SIZE,
                                            (row + 2.5 + 0.5 * (direction == 0)) * self.ROW_SIZE + 0 * (
                                                    direction == 1) - 1 * (direction == 0),
                                            fill=color, width=0)
        # Bit of a hack here
        if color == self.PREFERRED_GRAY:
            self.render_canvas.create_line((column + 1.65) * self.COLUMN_SIZE, (row + 2.45 + 0.25) * self.ROW_SIZE,
                                           (column + 1.75) * self.COLUMN_SIZE, (row + 2.10 + 0.25) * self.ROW_SIZE,
                                           (column + 1.85) * self.COLUMN_SIZE, (row + 2.40 + 0.25) * self.ROW_SIZE,
                                           (column + 1.95) * self.COLUMN_SIZE, (row + 2.15 + 0.25) * self.ROW_SIZE,
                                           (column + 2.05) * self.COLUMN_SIZE, (row + 2.35 + 0.25) * self.ROW_SIZE,
                                           (column + 2.15) * self.COLUMN_SIZE, (row + 2.20 + 0.25) * self.ROW_SIZE,
                                           (column + 2.25) * self.COLUMN_SIZE, (row + 2.30 + 0.25) * self.ROW_SIZE,
                                           (column + 2.35) * self.COLUMN_SIZE, (row + 2.25 + 0.25) * self.ROW_SIZE)
        else:
            self.render_canvas.create_line((column + 1.65) * self.COLUMN_SIZE, (row + 2.45) * self.ROW_SIZE,
                                           (column + 1.75) * self.COLUMN_SIZE, (row + 2.10) * self.ROW_SIZE,
                                           (column + 1.85) * self.COLUMN_SIZE, (row + 2.40) * self.ROW_SIZE,
                                           (column + 1.95) * self.COLUMN_SIZE, (row + 2.15) * self.ROW_SIZE,
                                           (column + 2.05) * self.COLUMN_SIZE, (row + 2.35) * self.ROW_SIZE,
                                           (column + 2.15) * self.COLUMN_SIZE, (row + 2.20) * self.ROW_SIZE,
                                           (column + 2.25) * self.COLUMN_SIZE, (row + 2.30) * self.ROW_SIZE,
                                           (column + 2.35) * self.COLUMN_SIZE, (row + 2.25) * self.ROW_SIZE)
        self.check_bus_clash(row, column, 'write')

    # Draws a tail bit slot
    def draw_guard(self, row, column, color):
        if not isinstance(row, int):
            raise TypeError('Expected int for row')
        if not isinstance(column, int):
            raise TypeError('Expected int for column')
        if not isinstance(color, str):
            raise TypeError('Expected str for color')
        direction = 1
        self.render_canvas.create_rectangle((column + 1.5) * self.COLUMN_SIZE,
                                            (row + 2 + 0.5 * (direction == 0)) * self.ROW_SIZE + 2 * (
                                                    direction == 1) + 0 * (direction == 0),
                                            (column + 2.5) * self.COLUMN_SIZE,
                                            (row + 2.5 + 0.5 * (direction == 0)) * self.ROW_SIZE + 0 * (
                                                    direction == 1) - 1 * (direction == 0),
                                            fill=color, width=0)
        self.render_canvas.create_text((column + 2) * self.COLUMN_SIZE,
                                       (row + 2.25) * self.ROW_SIZE,
                                       font=(self.APP_FONT, self.TEXT_SIZE - 2),
                                       justify=tk.CENTER,
                                       text='GRD')
        self.check_bus_clash(row, column, 'write')

    def check_bus_clash(self, row, column, bit_slot_type):
        if not isinstance(row, int):
            raise TypeError('Expected int for row')
        if not isinstance(column, int):
            raise TypeError('Expected int for column')
        if not isinstance(bit_slot_type, str):
            raise TypeError('Expected str for bit_slot_type')

        bit_slot = column + row * self.interface.columns_per_row

        # write
        if bit_slot_type == 'write':
            # Check if this bit slot is already driven
            if bit_slot in self.bit_slots_source or bit_slot in self.bit_slots_turnaround:
                # bus clash
                if bit_slot not in self.bit_slots_source_clashed:
                    self.bit_slots_source_clashed.append(bit_slot)
                self.render_canvas.create_rectangle((column + 1.5) * self.COLUMN_SIZE,
                                                    (row + 2) * self.ROW_SIZE + 2,
                                                    (column + 2.5) * self.COLUMN_SIZE,
                                                    (row + 2.5) * self.ROW_SIZE,
                                                    fill='black', width=0)
            else:
                # No clash
                self.bit_slots_source.append(bit_slot)
        # read
        elif bit_slot_type == 'read':
            # Check if this bit slot is already read
            if bit_slot in self.bit_slots_sink or bit_slot in self.bit_slots_turnaround:
                # bus clash
                if bit_slot not in self.bit_slots_sink_clashed:
                    self.bit_slots_sink_clashed.append(bit_slot)
                self.render_canvas.create_rectangle((column + 1.5) * self.COLUMN_SIZE,
                                                    (row + 2.5) * self.ROW_SIZE,
                                                    (column + 2.5) * self.COLUMN_SIZE,
                                                    (row + 3.0) * self.ROW_SIZE - 1,
                                                    fill='red', width=0)
            else:
                self.bit_slots_sink.append(bit_slot)

        # handover
        elif bit_slot_type == 'handover':
            # Check if this bit slot is already read
            if bit_slot in self.bit_slots_sink:
                # bus clash
                if bit_slot not in self.bit_slots_sink_clashed:
                    self.bit_slots_sink_clashed.append(bit_slot)
                self.render_canvas.create_rectangle((column + 1.5) * self.COLUMN_SIZE,
                                                    (row + 2.5) * self.ROW_SIZE,
                                                    (column + 2.5) * self.COLUMN_SIZE,
                                                    (row + 3.0) * self.ROW_SIZE - 1,
                                                    fill='red', width=0)
            if bit_slot in self.bit_slots_source:
                # bus clash
                if bit_slot not in self.bit_slots_source_clashed:
                    self.bit_slots_source_clashed.append(bit_slot)
                self.render_canvas.create_rectangle((column + 1.5) * self.COLUMN_SIZE,
                                                    (row + 2) * self.ROW_SIZE + 2,
                                                    (column + 2.5) * self.COLUMN_SIZE,
                                                    (row + 2.5) * self.ROW_SIZE,
                                                    fill='black', width=0)

            if bit_slot not in self.bit_slots_turnaround:
                self.bit_slots_turnaround.append(bit_slot)

    def write_bit_slot(self, row, column, source, text, color):
        if not isinstance(source, bool):
            raise TypeError('Expected bool for source')
        if not isinstance(row, int):
            raise TypeError('Expected int for row')
        if not isinstance(column, int):
            raise TypeError('Expected int for column')
        if not isinstance(text, str):
            raise TypeError('Expected str for text')
        if not isinstance(color, str):
            raise TypeError('Expected str for color')

        # Add channel & bit numbers to each bit slot, source 1 = write
        self.render_canvas.create_rectangle((column + 1.5) * self.COLUMN_SIZE,
                                            (row + 2 + 0.5 * (not source)) * self.ROW_SIZE + 2 * (
                                                source) + 0 * (not source),
                                            (column + 2.5) * self.COLUMN_SIZE,
                                            (row + 2.5 + 0.5 * (not source)) * self.ROW_SIZE + 0 * (
                                                source) - 1 * (not source),
                                            fill=color, width=0)
        self.render_canvas.create_text((column + 2) * self.COLUMN_SIZE,
                                       (row + 2.25 + 0.5 * (not source)) * self.ROW_SIZE + 0 * (
                                           source) - 1 * (not source),
                                       font=(self.APP_FONT, self.TEXT_SIZE - 2),
                                       justify=tk.CENTER,
                                       text=text)
        if source:
            self.check_bus_clash(row, column, 'write')
        else:
            self.check_bus_clash(row, column, 'read')

    # Draws one data port in a canvas
    def draw_data_port(self, data_port, color):

        if not isinstance(data_port, DataPort):
            raise TypeError('Expected DataPort object, got: ' + str(type(data_port)))
        if not isinstance(color, str):
            raise TypeError('Expected str for color')

        # Check ranges of input parameters, should be ok based on earlier checking
        error_text = ''
        if data_port.channels < DataPort.MIN_CHANNELS or data_port.channels > DataPort.MAX_CHANNELS:
            error_text += 'Channel count out of range\n'
        if data_port.channel_grouping < DataPort.MIN_CHANNEL_GROUPING or data_port.channel_grouping > DataPort.MAX_CHANNEL_GROUPING:
            error_text += 'Channel Grouping out of range\n'
        if data_port.channel_group_spacing < DataPort.MIN_CHANNEL_GROUP_SPACING or data_port.channel_group_spacing > \
                DataPort.MAX_CHANNEL_GROUP_SPACING:
            error_text += 'Change Group Spacing out of range\n'
        if data_port.sample_width < DataPort.MIN_SAMPLE_WIDTH or data_port.sample_width > DataPort.MAX_SAMPLE_WIDTH:
            error_text += 'Sample Width out of range\n'
        if data_port.sample_grouping < DataPort.MIN_SAMPLE_GROUPING or data_port.sample_grouping > DataPort.MAX_SAMPLE_GROUPING:
            error_text += 'Sample Grouping out of range\n'
        if data_port.interval_integer < DataPort.MIN_INTERVAL_INTEGER or data_port.interval_integer > DataPort.MAX_INTERVAL_INTEGER:
            error_text += 'Interval out of range\n'
        if data_port.interval_numerator < DataPort.MIN_INTERVAL_NUMERATOR or data_port.interval_numerator > DataPort.MAX_INTERVAL_NUMERATOR:
            error_text += 'Fractional Interval out of range\n'
        if data_port.offset < DataPort.MIN_OFFSET or data_port.offset > DataPort.MAX_OFFSET:
            error_text += 'Offset out of range\n'
        if data_port.h_start < 0 or data_port.h_start >= self.interface.MAX_COLUMNS:
            error_text += 'Horizontal Start out of range\n'
        if data_port.h_stop < 0 or data_port.h_stop >= self.interface.MAX_COLUMNS:
            error_text += 'Horizontal Stop out of range\n'

        # Check some relationships
        if data_port.offset > data_port.interval_integer:
            error_text += 'Offset > Interval\n'
        if data_port.h_start >= self.interface.columns_per_row:
            error_text += 'Horizontal Start > Columns per Row\n'
        if data_port.h_stop >= self.interface.columns_per_row:
            error_text += 'Horizontal Stop > Columns per Row\n'
        if data_port.h_stop < data_port.h_start:
            error_text += 'Horizontal Stop < Horizontal Start\n'
        if self.interface.handover_width > data_port.h_start and data_port.source and data_port.handover:
            error_text += 'TA width > Horizontal Start\n'
        if self.interface.tail_width > self.interface.columns_per_row - data_port.h_stop and data_port.source and data_port.tail:
            error_text += 'Tail width would overflow row\n'
        if data_port.h_stop >= self.interface.columns_per_row and data_port.source and data_port.guard:
            error_text += 'Post zero would overflow row\n'

        if len(error_text) > 0:
            return error_text

        horizontal_width = data_port.h_stop - data_port.h_start + 1

        # Raster our frame
        for row_counter in range(data_port.offset * self.interface.interval_denominator,
                                 self.rows_in_frame * self.interface.interval_denominator,
                                 self.interface.interval_denominator * data_port.interval_integer + data_port.interval_numerator):
            interval_start_row = int(math.ceil(row_counter / self.interface.interval_denominator))
            interval_next_row = int(math.ceil(
                (row_counter + self.interface.interval_denominator * data_port.interval_integer + data_port.interval_numerator) /
                self.interface.interval_denominator))

            # Re-arm the data port here
            bits_remaining = data_port.sample_width
            samples_remaining = data_port.sample_grouping
            channels_remaining = data_port.channels
            if data_port.channel_grouping == 0 or data_port.channel_grouping > data_port.channels:
                effective_channel_group = data_port.channels
            else:
                effective_channel_group = data_port.channel_grouping
            channel_groups_counter = effective_channel_group

            for data_port_row in range(0, interval_next_row - interval_start_row):
                next_column = 0
                for data_port_column in range(0, horizontal_width):
                    # Do we drove this bit slot?
                    if samples_remaining > 0 and channels_remaining > 0 and bits_remaining > 0 and data_port_column == next_column:

                        end_of_row = 0
                        channel = data_port.channels - channels_remaining + 1

                        if data_port_column == 0 and data_port.handover and data_port.source:
                            # We have turnarounds to write
                            for count in range(0, self.interface.handover_width):
                                self.draw_handover(interval_start_row + data_port_row, (data_port.h_start - count - 1))

                        self.write_bit_slot(interval_start_row + data_port_row, data_port.h_start + data_port_column, data_port.source,
                                            'c' + str(channel) +
                                            'b' + str(bits_remaining - 1), color)

                        # We've written a bit slot, decrement bits remaining
                        bits_remaining -= 1
                        next_column += 1

                        # Order of iterators: bits, channels in a channel group, samples in a group, finally channel groups

                        # We've drawn all bits in a sample
                        if bits_remaining == 0:
                            if samples_remaining >= 1:
                                # Decrement channels in our channel group
                                channels_remaining -= 1
                                bits_remaining = data_port.sample_width

                                # Done with channels in a group?
                                if channels_remaining == data_port.channels - channel_groups_counter:
                                    channels_remaining = data_port.channels + effective_channel_group - channel_groups_counter
                                    # Decrement samples in our sample group
                                    samples_remaining -= 1

                                # Done with samples in a group?
                                if samples_remaining == 0:
                                    samples_remaining = data_port.sample_grouping
                                    channels_remaining -= effective_channel_group
                                    # In case channels_remaining is less than channel_groups
                                    effective_channel_group = min(effective_channel_group, channels_remaining)
                                    # Next channel group
                                    channel_groups_counter += effective_channel_group

                                    # Channel-group spacing
                                    if data_port.channel_group_spacing != 0:
                                        # Hold off writing for (data_port.channel_group_spacing - 1) columns if needed
                                        next_column = data_port_column + data_port.channel_group_spacing
                                    else:
                                        # Wait until the next row
                                        next_column = horizontal_width
                                        end_of_row = 1
                        if next_column >= horizontal_width or end_of_row:
                            # We have a guard bitslot to write?
                            if (data_port_column == horizontal_width - 1 or end_of_row) and data_port.guard and data_port.source:
                                self.draw_guard(interval_start_row + data_port_row, (data_port.h_start + data_port_column
                                                                                        + 1), color)
                            # We have tails to write?
                            if (data_port_column == horizontal_width - 1 or end_of_row) and data_port.tail and data_port.source:
                                for count in range(data_port.guard, self.interface.tail_width + data_port.guard):
                                    self.draw_tail(interval_start_row + data_port_row, (data_port.h_start + data_port_column
                                                                                        + count + 1), color)

        return ''

    def create_entry(self, validate_command):
        return tk.Entry(self.config_frame, width=self.NUMBER_ENTRY_WIDTH, validate='key', validatecommand=validate_command,
                        justify=tk.CENTER, font="TkDefaultFont " + str(self.TEXT_SIZE), relief=self.ENTRYBOX_RELIEF, bd=1,
                        highlightcolor=self.PREFERRED_GRAY, highlightthickness=1)


class Interface:
    # Frame parameters
    MIN_COLUMNS_PER_ROW = 2
    MAX_COLUMNS_PER_ROW = 32
    MIN_COLUMNS = 1
    MAX_COLUMNS = MAX_COLUMNS_PER_ROW
    MIN_ROWS = 1
    MAX_ROWS = 10240
    MIN_ROW_RATE = 1
    MAX_ROW_RATE = 6144
    MIN_INTERVAL_DENOMINATOR = 1
    MAX_INTERVAL_DENOMINATOR = 512
    MIN_BULK_HORIZONTAL_START = 1
    MAX_BULK_HORIZONTAL_START = MAX_COLUMNS - 2
    BULK_WIDTH = [0, 2, 5, 10]
    MIN_S0_WIDTH = 1
    MAX_S0_WIDTH = 2
    MIN_HANDOVER_WIDTH = 0
    MAX_HANDOVER_WIDTH = 2
    MIN_TAIL_WIDTH = 0
    MAX_TAIL_WIDTH = 2

    NUM_DATA_PORTS = 12

    def __init__(self):
        self.columns_per_row = Interface.MAX_COLUMNS
        self.s0s1_enabled = True
        self.s0_width = Interface.MIN_S0_WIDTH
        self.s0_handover_enabled = True
        self.cds_guard_enabled = False
        self.handover_width = 1
        self.tail_width = Interface.MIN_TAIL_WIDTH
        self.interval_denominator = 16
        self.bulk_horizontal_start = 2
        self.bulk_width = min(Interface.BULK_WIDTH)
        self.bulk_guard_enabled = False
        self.row_rate = 3072
        self._interval_lcm = 0
        self.data_ports = []
        for count in range(0, self.NUM_DATA_PORTS):
            self.data_ports.append(DataPort())

    @property
    def columns_per_row(self):
        return self._columns_per_row

    @columns_per_row.setter
    def columns_per_row(self, v):
        if type(v) != int:
            raise TypeError('Max columns must be int, got ' + str(type(v)))
        if v > Interface.MAX_COLUMNS:
            raise ValueError('Columns per row must be <= ' + str(Interface.MAX_COLUMNS))
        if v < Interface.MIN_COLUMNS:
            raise ValueError('Columns per row must be >= ' + str(Interface.MIN_COLUMNS))
        self._columns_per_row = v

    @property
    def s0s1_enabled(self):
        return self._s0s1_enabled

    @s0s1_enabled.setter
    def s0s1_enabled(self, v):
        if type(v) != bool:
            raise TypeError('S0/S1 enabled must be bool, got ' + str(type(v)))
        self._s0s1_enabled = bool(v)

    @property
    def s0_width(self):
        return self._s0_width

    @s0_width.setter
    def s0_width(self, v):
        if type(v) != int:
            raise TypeError('S0 width must be int, got ' + str(type(v)))
        if v > Interface.MAX_S0_WIDTH:
            raise ValueError('S0 width must be <= ' + str(Interface.MAX_S0_WIDTH))
        if v < Interface.MIN_S0_WIDTH:
            raise ValueError('S0 width must be >= ' + str(Interface.MIN_S0_WIDTH))
        self._s0_width = v

    @property
    def s0_handover_enabled(self):
        return self._s0_handover_enabled

    @s0_handover_enabled.setter
    def s0_handover_enabled(self, v):
        if type(v) != bool:
            raise TypeError('S0 handover enabled must be bool, got ' + str(type(v)))
        self._s0_handover_enabled = bool(v)

    @property
    def cds_guard_enabled(self):
        return self._cds_guard_enabled

    @cds_guard_enabled.setter
    def cds_guard_enabled(self, v):
        if type(v) != bool:
            raise TypeError('CDS guard enabled must be bool, got ' + str(type(v)))
        self._cds_guard_enabled = bool(v)

    @property
    def handover_width(self):
        return self._handover_width

    @handover_width.setter
    def handover_width(self, v):
        if type(v) != int:
            raise TypeError('Handover width must be int, got ' + str(type(v)))
        if v > Interface.MAX_HANDOVER_WIDTH:
            raise ValueError('Handover width must be <= ' + str(Interface.MAX_HANDOVER_WIDTH))
        if v < Interface.MIN_HANDOVER_WIDTH:
            raise ValueError('Handover width must be >= ' + str(Interface.MIN_HANDOVER_WIDTH))
        self._handover_width = v

    @property
    def tail_width(self):
        return self._tail_width

    @tail_width.setter
    def tail_width(self, v):
        if type(v) != int:
            raise TypeError('Tail width must be int, got ' + str(type(v)))
        if v > Interface.MAX_TAIL_WIDTH:
            raise ValueError('Tail width must be <= ' + str(Interface.MAX_TAIL_WIDTH))
        if v < Interface.MIN_TAIL_WIDTH:
            raise ValueError('Handover width must be >= ' + str(Interface.MIN_TAIL_WIDTH))
        self._tail_width = v

    @property
    def interval_denominator(self):
        return self._interval_denominator

    @interval_denominator.setter
    def interval_denominator(self, v):
        if type(v) != int:
            raise TypeError('Interval denominator must be int, got ' + str(type(v)))
        if v > Interface.MAX_INTERVAL_DENOMINATOR:
            raise ValueError('Interval denominator must be <= ' + str(Interface.MAX_INTERVAL_DENOMINATOR))
        if v < Interface.MIN_INTERVAL_DENOMINATOR:
            raise ValueError('Interval denominator must be >= ' + str(Interface.MIN_INTERVAL_DENOMINATOR))
        self._interval_denominator = v

    @property
    def bulk_horizontal_start(self):
        return self._bulk_horizontal_start

    @bulk_horizontal_start.setter
    def bulk_horizontal_start(self, v):
        if type(v) != int:
            raise TypeError('Bulk horizontal start must be int, got ' + str(type(v)))
        if v > Interface.MAX_BULK_HORIZONTAL_START:
            raise ValueError('Bulk horizontal start must be <= ' + str(Interface.MAX_BULK_HORIZONTAL_START))
        if v < Interface.MIN_BULK_HORIZONTAL_START:
            raise ValueError('Bulk horizontal start must be >= ' + str(Interface.MIN_BULK_HORIZONTAL_START))
        self._bulk_horizontal_start = v

    @property
    def bulk_width(self):
        return self._bulk_width

    @bulk_width.setter
    def bulk_width(self, v):
        if type(v) != int:
            raise TypeError('Bulk width must be int, got ' + str(type(v)))
        if v not in Interface.BULK_WIDTH:
            raise ValueError('Bulk width start must be' + str(Interface.BULK_WIDTH))
        self._bulk_width = v

    @property
    def bulk_guard_enabled(self):
        return self._bulk_guard_enabled

    @bulk_guard_enabled.setter
    def bulk_guard_enabled(self, v):
        if type(v) != bool:
            raise TypeError('Bulk guard enabled must be bool, got ' + str(type(v)))
        self._bulk_guard_enabled = bool(v)

    @property
    def row_rate(self):
        return self._row_rate

    @row_rate.setter
    def row_rate(self, v):
        if type(v) != int:
            raise TypeError('Row rate must be int, got ' + str(type(v)))
        if v > Interface.MAX_ROW_RATE:
            raise ValueError('Row must be <= ' + str(Interface.MAX_ROW_RATE))
        if v < Interface.MIN_ROW_RATE:
            raise ValueError('Row rate must be >= ' + str(Interface.MIN_ROW_RATE))
        self._row_rate = v

    @property
    def interval_lcm(self):
        # This will be calculated rather than be an instance variable
        interval_list = []
        for count, data_port in enumerate(self.data_ports):
            if data_port.enabled:
                interval_list.append(
                    self.data_ports[count].interval_integer * self.interval_denominator + self.data_ports[
                        count].interval_numerator)
            interval_list.append(self.interval_denominator)

        self._interval_lcm = interval_list[0]
        for i in interval_list[1:]:
            if sys.version_info[0] == 3:
                self._interval_lcm = int(self._interval_lcm * i / math.gcd(self._interval_lcm, i))
            else:
                self._interval_lcm = int(self._interval_lcm * i / fractions.gcd(self._interval_lcm, i))
        self._interval_lcm = int(self._interval_lcm / self.interval_denominator)
        return self._interval_lcm


class DataPort:
    count = 0

    # Data port ranges
    MIN_CHANNELS = 1
    MAX_CHANNELS = 16
    MIN_CHANNEL_GROUPING = 0
    MAX_CHANNEL_GROUPING = MAX_CHANNELS - 1
    MIN_CHANNEL_GROUP_SPACING = 0
    MAX_CHANNEL_GROUP_SPACING = MAX_CHANNELS - 1
    MIN_SAMPLE_WIDTH = 1
    MAX_SAMPLE_WIDTH = 64
    MIN_SAMPLE_GROUPING = 1
    MAX_SAMPLE_GROUPING = 8
    MIN_INTERVAL_INTEGER = 1
    MAX_INTERVAL_INTEGER = 1024
    MIN_INTERVAL_NUMERATOR = 0
    MAX_INTERVAL_NUMERATOR = Interface.MAX_INTERVAL_DENOMINATOR - 1
    MIN_OFFSET = 0
    MAX_OFFSET = MAX_INTERVAL_INTEGER - 1
    MIN_H_START = 1
    MAX_H_START = Interface.MAX_COLUMNS
    MIN_H_STOP = 1
    MAX_H_STOP = Interface.MAX_COLUMNS

    NUM_DP_PARAMETERS = 10

    def __init__(self):
        self.name = 'DP' + str(DataPort.count)
        self.channels = 1
        self.channel_grouping = 0
        self.channel_group_spacing = 0
        self.sample_width = 16
        self.sample_grouping = 1
        self.interval_integer = 64
        self.interval_numerator = 0
        self.offset = 0
        self.h_start = 4
        self.h_stop = 10
        self.source = True
        self.handover = True
        self.tail = False
        self.guard = False
        self.enabled = False
        self.sample_rate = 48.0
        DataPort.count += 1

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, v):
        if type(v) != str:
            raise TypeError('Name must be a string, got ' + str(type(v)))
        self._name = v

    @property
    def channels(self):
        return self._channels

    @channels.setter
    def channels(self, v):
        if type(v) != int:
            raise TypeError('Channels must be int, got ' + str(type(v)))
        if v > DataPort.MAX_CHANNELS:
            raise ValueError('Channels must be <= ' + str(DataPort.MAX_CHANNELS))
        if v < DataPort.MIN_CHANNELS:
            raise ValueError('Channels must be >= ' + str(DataPort.MIN_CHANNELS))
        self._channels = v

    @property
    def channel_grouping(self):
        return self._channel_grouping

    @channel_grouping.setter
    def channel_grouping(self, v):
        if type(v) != int:
            raise TypeError('Channel grouping must be int, got ' + str(type(v)))
        if v > DataPort.MAX_CHANNEL_GROUPING:
            raise ValueError('Channel grouping must be <= ' + str(DataPort.MAX_CHANNEL_GROUPING))
        if v < DataPort.MIN_CHANNEL_GROUPING:
            raise ValueError('Channel grouping be >= ' + str(DataPort.MIN_CHANNEL_GROUPING))
        self._channel_grouping = v

    @property
    def channel_group_spacing(self):
        return self._channel_group_spacing

    @channel_group_spacing.setter
    def channel_group_spacing(self, v):
        if type(v) != int:
            raise TypeError('Channel group spacing must be int, got ' + str(type(v)))
        if v > DataPort.MAX_CHANNEL_GROUP_SPACING:
            raise ValueError('Channel group spacing must be <= ' + str(DataPort.MAX_CHANNEL_GROUP_SPACING))
        if v < DataPort.MIN_CHANNEL_GROUP_SPACING:
            raise ValueError('Channel group spacing must be >= ' + str(DataPort.MIN_CHANNEL_GROUP_SPACING))
        self._channel_group_spacing = v

    @property
    def sample_width(self):
        return self._sample_width

    @sample_width.setter
    def sample_width(self, v):
        if type(v) != int:
            raise TypeError('Sample width must be int, got ' + str(type(v)))
        if v > DataPort.MAX_SAMPLE_WIDTH:
            raise ValueError('Sample width must be <= ' + str(DataPort.MAX_SAMPLE_WIDTH))
        if v < DataPort.MIN_SAMPLE_WIDTH:
            raise ValueError('Sample width must be >= ' + str(DataPort.MIN_SAMPLE_WIDTH))
        self._sample_width = v

    @property
    def sample_grouping(self):
        return self._sample_grouping

    @sample_grouping.setter
    def sample_grouping(self, v):
        if type(v) != int:
            raise TypeError('Sample grouping must be int, got ' + str(type(v)))
        if v > DataPort.MAX_SAMPLE_GROUPING:
            raise ValueError('Sample grouping must be <= ' + str(DataPort.MAX_SAMPLE_GROUPING))
        if v < DataPort.MIN_SAMPLE_GROUPING:
            raise ValueError('Sample grouping must be >= ' + str(DataPort.MIN_SAMPLE_GROUPING))
        self._sample_grouping = v

    @property
    def interval_integer(self):
        return self._interval_integer

    @interval_integer.setter
    def interval_integer(self, v):
        if type(v) != int:
            raise TypeError('Interval integer must be int, got ' + str(type(v)))
        if v > DataPort.MAX_INTERVAL_INTEGER:
            raise ValueError('Interval integer must be <= ' + str(DataPort.MAX_INTERVAL_INTEGER))
        if v < DataPort.MIN_INTERVAL_INTEGER:
            raise ValueError('Interval integer must be >= ' + str(DataPort.MIN_INTERVAL_INTEGER))
        self._interval_integer = v

    @property
    def interval_numerator(self):
        return self._interval_numerator

    @interval_numerator.setter
    def interval_numerator(self, v):
        if type(v) != int:
            raise TypeError('Interval must be int, got ' + str(type(v)))
        if v > DataPort.MAX_INTERVAL_NUMERATOR:
            raise ValueError('Interval must be <= ' + str(DataPort.MAX_INTERVAL_NUMERATOR))
        if v < DataPort.MIN_INTERVAL_NUMERATOR:
            raise ValueError('Interval must be >= ' + str(DataPort.MIN_INTERVAL_NUMERATOR))
        self._interval_numerator = v

    @property
    def offset(self):
        return self._offset

    @offset.setter
    def offset(self, v):
        if type(v) != int:
            raise TypeError('Offset must be int, got ' + str(type(v)))
        if v > DataPort.MAX_OFFSET:
            raise ValueError('Offset must be <= ' + str(DataPort.MAX_OFFSET))
        if v < DataPort.MIN_OFFSET:
            raise ValueError('Offset must be >= ' + str(DataPort.MIN_OFFSET))
        self._offset = v

    @property
    def h_start(self):
        return self._h_start

    @h_start.setter
    def h_start(self, v):
        if type(v) != int:
            raise TypeError('Horizontal start must be int, got ' + str(type(v)))
        if v > DataPort.MAX_H_START:
            raise ValueError('Horizontal start must be <= ' + str(DataPort.MAX_H_START))
        if v < DataPort.MIN_H_START:
            raise ValueError('Horizontal start must be >= ' + str(DataPort.MIN_H_START))
        self._h_start = v

    @property
    def h_stop(self):
        return self._h_stop

    @h_stop.setter
    def h_stop(self, v):
        if type(v) != int:
            raise TypeError('Horizontal stop must be int, got ' + str(type(v)))
        if v > DataPort.MAX_H_STOP:
            raise ValueError('Horizontal stop must be <= ' + str(DataPort.MAX_H_STOP))
        if v < DataPort.MIN_H_STOP:
            raise ValueError('Horizontal stop must be >= ' + str(DataPort.MIN_H_STOP))
        self._h_stop = v

    @property
    def source(self):
        return self._source

    @source.setter
    def source(self, v):
        if type(v) != bool:
            raise TypeError('Source enabled must be bool, got ' + str(type(v)))
        self._source = bool(v)

    @property
    def handover(self):
        return self._handover

    @handover.setter
    def handover(self, v):
        if type(v) != bool:
            raise TypeError('Handover enabled must be bool, got ' + str(type(v)))
        self._handover = bool(v)

    @property
    def tail(self):
        return self._tail

    @tail.setter
    def tail(self, v):
        if type(v) != bool:
            raise TypeError('Tail enabled must be bool, got ' + str(type(v)))
        self._tail = bool(v)

    @property
    def guard(self):
        return self._guard

    @guard.setter
    def guard(self, v):
        if type(v) != bool:
            raise TypeError('Guard enabled must be bool, got ' + str(type(v)))
        self._guard = bool(v)

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, v):
        if type(v) != bool:
            raise TypeError('Data port enabled must be bool, got ' + str(type(v)))
        self._enabled = bool(v)


if __name__ == '__main__':
    root = tk.Tk()
    app = App(root)
    while True:
        try:
            app.mainloop()
            break
        except UnicodeDecodeError:
            # Catches a known TCL/TK issues
            pass
