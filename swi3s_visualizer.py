
"""
Copyright (c) 2020-2022 MIPI Alliance and other contributors. All Rights Reserved.

Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice, this list of conditions and the following disclaimer in the documentation and/or other materials provided with the distribution.

3. Neither the name of the copyright holder nor the names of its contributors may be used to endorse or promote products derived from this software without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

# Break lines

# To Do
# Support odd columne counts.
# Add a calculator of the Visualizer that shows the value that would be written the bit clock to sample clock register
# Channel group spacing breaks guards an tails even with channel grouping = 0
# Error when horizontal_count is too small? Hard to tell.
# Handle the specific detail of manager as data source near the end of a row (just before S0).
# Add a field to control CDS_Width.
# Tidy up (maybe) the spacing of the check boxes.
# Allow the control column to be anywhere (including just before S0).

# Reconcile Device Numbers and 'is Manager' attributes.
# make guard and tail rules contemplate Device number and not just data ports.

# ToDos that might want to start with a re-write:
# Investigate and remote "Draw S0 Handover".Fix S0 handover (preceding bit)
# Have the denominator also change witth the UI view flag.
# Widen the range for S0. - done.
# Change the name of "Interval Denominator" to skipping.
# Fix the offset betwen the label for sample rate and the values.
# break dataport reset into two functions and rename so that prepare/enable/SSP portion is clear.
# Have a way to indicate that two dataports are in the same device (and thus the guards and tails are impacted).
# Fold up the check boxes
# Add CDS Start control and S1 tails and rename  "CDS/S0 Handover Width" (actually controlling the S1 tail) to be S1 tail width.
# Change the symbol between S1 and Control to show the funny Manager handover
# Read in wav files per data port and place in output
# Add Flow-control bits (including the runt port).

# Issue with wide bits missing guards and tails missing on some rows fixed
# Added error checking for h_width + 1 mod bit_width + 1 = 0
# Fixed tail & guard bits when using channel grouping. Now after the last bitslot driven in each row.

# Not TODO (mostly because it does not make sense):
# Fix first row so that complementary sample rate can share an interval (e.g. 23 and 25 kHz can exist in the same 48 kHz interval).

from __future__ import division  # Fixes a Python 2 issue with integer division

import sys
import io
import math
import csv
import os
import platform
import distutils.util

import argparse
import re
from enum import Enum
import json

sys.path.insert( 0, "/Library/Frameworks/Python.framework/Versions/3.9/lib" )

#Strings that are use in file loading and storing
SAVE_CODING_STRING = 'Save file using excess one'
DATA_PORT_NAME = 'Data Port Name'
DATA_PORT_DEVICE_NUMBER = 'Data Port Device Number'
DATA_PORT_CHANNELS = 'Data Port Channels'
DATA_PORT_CHANNEL_GROUPING = 'Data Port Channel Grouping'
DATA_PORT_CHANNEL_GROUP_SPACING = 'Data Port Channel Group Spacing'
DATA_PORT_SAMPLE_WIDTH = 'Data Port Sample Width'
DATA_PORT_SAMPLE_GROUPING = 'Data Port Sample Grouping'
DATA_PORT_INTERVAL = 'Data Port Interval Integer'
DATA_PORT_SKIPPING_NUMERATOR = 'Data Port Interval Numerator'
DATA_PORT_OFFSET = 'Data Port Offset'
DATA_PORT_HORIZONTAL_START = 'Data Port Horizontal Start'
DATA_PORT_HORIZONTAL_COUNT = 'Data Port Horizontal Count'
DATA_PORT_TAIL_WIDTH = 'Data Port Tail Width'
DATA_PORT_BIT_WIDTH = 'Data Port Bit Width'
DATA_PORT_IS_SOURCE = 'Source'
DATA_PORT_DRAW_HANDOVER = 'Draw Data Port Handover'
DATA_PORT_GUARD_ENABLED = 'Data Port Guard Enabled'
DATA_PORT_GUARD_POLARITY = 'Data Port Guard Polarity'
DATA_PORT_ENABLED = 'Data Port Enabled'
DATA_PORT_IN_MANAGER = 'Data Port In Manager'
DATA_PORT_SRI =  'Data Port DRI'


NOT_OWNED = 'not owned'

finished_drawing = False

# in C these would be compilation switches.  Here they are controlling blocks of code.
SRI_USES_CHANNEL_GROUPING_ONLY = False
SRI_USES_SAMPLE_COUNT = True
FRACTION_IS_DITHERED_TRANSPORT_INTERVAL = False
SAVE_FILE_USING_EXCESS_ONE = True
Debug_Drawing = False
Debug_FileIO = False

# While currently used as a switch, these varibles might by controlled by the UI should someone add a widget to do so:
UI_IS_EXCESS_1 = True

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
    print( messagebox )
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


GuardText = 'G'

###############################
#                             #
#    FRAME MODEL CLASSES      #
#                             #
###############################

class Frame_model:

    def __init__(self, n_rows=0, n_cols=2):
        self.row_info = []
        for i in range(0, n_rows):
            self.row_info.append(Row_info(n_cols, i))            

    def append_row(self, row):
        self.row_info.append(row)

    def get_row(self, i):
        return self.row_info[i]

class Row_info:

    def __init__(self, n_col, row_number):
        self.row_num = row_number
        self.col_info = [Col_info(i) for i in range(0, n_col)]

    def get_col(self, i):
        return self.col_info[i]
        
class Col_info:

    def __init__(self, col_number):
        self.col_num = col_number
        self.slot_info = []

    def append_slot(self, slot):
        self.slot_info.append(slot)

class Slot_info:

    def __init__(self):
        self.slot_type  = Slot_type.NORMAL
        self.dir        = Direction_type.SINK
        self.device_num = 0
        self.dp_num     = 0
        self.channel    = 0
        self.sample     = 0
        self.bit_num    = 0

class Direction_type(Enum):
    SOURCE = 0
    SINK   = 1

class Slot_type(Enum):
    NORMAL   = 0
    GUARD    = 1
    TAIL     = 2
    HANDOVER = 3
    CDS      = 4
    S0       = 5
    S1       = 6


class SimpleJSONEncoder(json.JSONEncoder):
   def default(self, obj):
        if hasattr(obj, "__dict__"):
            d = {}
            for key, value in obj.__dict__.items():
                if not key.startswith("_"):
                    if (isinstance(value, Enum)):
                        d[key] = value.name
                    else:
                        d[key] = value
            return d
        return super().default(obj)

###############################


class App(tk.Frame):
    def __init__(self, master, args):
        tk.Frame.__init__(self, master)
        # root is window

### Cut from here for Eddie
        self.VERSION = '1.63'
### To here for Eddie
        self.args = args
        self.frame_model = Frame_model()

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
                          '#BFFFBF', '#BFFFFF'] # Thanks to Eddie for the colours.

        self.INTERFACE_PARAMETER_TITLES = ['Columns per Row (' + str(Interface.MIN_COLUMNS_PER_ROW) + '-' + str(Interface.MAX_COLUMNS_PER_ROW)
                                           + ')',
                                           'S0 S1 Enabled',
                                           'S0 Width (' + str(Interface.MIN_S0_WIDTH) + '-' + str(Interface.MAX_S0_WIDTH) + ')',
                                           # 'S1 Tails',
                                           'CDS Guard Enabled',
                                           'CDS Tail Width (' + str(Interface.MIN_TAIL_WIDTH) + '-' + str(Interface.MAX_TAIL_WIDTH) + ')',
                                           'Interval Denominator (' + str(Interface.MIN_SKIPPING_DENOMINATOR) + '-' + str(
                                               Interface.MAX_SKIPPING_DENOMINATOR) + ')',
                                           'CDS/S0 Handover Width (' + str(Interface.MIN_CDS_S0_HANDOVER_WIDTH) + '-' + str(
                                               Interface.MAX_CDS_S0_HANDOVER_WIDTH) + ')',
                                           'Draw S0 Handover',
                                           'Row Rate [kHz] (' + str(Interface.MIN_ROW_RATE) + '-' + str(Interface.MAX_ROW_RATE) + ')',
                                           'Rows to Draw (' + str(self.MIN_ROWS_IN_FRAME) + '-' + str(self.MAX_ROWS_IN_FRAME) + ')']

        self.DP_PARAMETER_DESCRIPTIONS = ['Device Number (' + str(DataPort.MIN_DEVICE_NUMBER) + '-' + str(DataPort.MAX_DEVICE_NUMBER) + ')',
                                          'Channels (' + str(DataPort.MIN_CHANNELS + ( 0 if UI_IS_EXCESS_1 else 1 ) ) + '-' + str(DataPort.MAX_CHANNELS + ( 0 if UI_IS_EXCESS_1 else 1 ) ) + ')',
                                          'Channel Grouping (' + str(DataPort.MIN_CHANNEL_GROUPING) + '-' + str(
                                              DataPort.MAX_CHANNEL_GROUPING) + ')',
                                          'Spacing (' + str(DataPort.MIN_CHANNEL_GROUP_SPACING) + '-' + str(
                                              DataPort.MAX_CHANNEL_GROUP_SPACING) + ')',
                                          'Sample Size (' + str(DataPort.MIN_SAMPLE_WIDTH + ( 0 if UI_IS_EXCESS_1 else 1 ) ) + '-' + str(DataPort.MAX_SAMPLE_WIDTH + ( 0 if UI_IS_EXCESS_1 else 1 ) ) + ')',
                                          'Sample Grouping (' + str(DataPort.MIN_SAMPLE_GROUPING + ( 0 if UI_IS_EXCESS_1 else 1 ) ) + '-' + str(
                                              DataPort.MAX_SAMPLE_GROUPING + ( 0 if UI_IS_EXCESS_1 else 1 ) ) + ')',
                                          'Interval (' + str(DataPort.MIN_INTERVAL + ( 0 if UI_IS_EXCESS_1 else 1 ) ) + '-' + str(
                                              DataPort.MAX_INTERVAL + ( 0 if UI_IS_EXCESS_1 else 1 ) ) + ')',
                                          'Skipping Numerator (' + str(DataPort.MIN_SKIPPING_NUMERATOR) + '-' + str(
                                              DataPort.MAX_SKIPPING_NUMERATOR) + ')',
                                          'Offset (' + str(DataPort.MIN_OFFSET) + '-' + str(DataPort.MAX_OFFSET) + ')',
                                          'Horizontal Start (0-' + str(Interface.MAX_COLUMNS - 1) + ')',
                                          'Horizontal Count (0-' + str(Interface.MAX_COLUMNS - 1) + ')',
                                          'Tail Width (0-' + str(Interface.MAX_TAIL_WIDTH) + ')',
                                          'WideBit Width (0-' + str(Interface.MAX_BIT_WIDTH) + ')',
                                          'Source [checked] / Sink',
                                          'Draw Handover',
                                          'Guard Enabled',
                                          'SRI',
                                          'Data Port Enabled',
                                          'Manager DataPort',
                                          'Calculated SR [kHz]']

        window_width = int(35.5 * self.COLUMN_SIZE)
        window_height = 800
        data_frame_height = 400
        canvas_height = 200
        self.canvas_width = int(35.5 * self.COLUMN_SIZE)

        self.interface = Interface()

        self.master.title('SoundWire I3S Payload Visualizer v' + self.VERSION)
        self.master.minsize(window_width, window_height)
        self.master.geometry("+150+50")
        self.master.resizable(False, True)
        self.master.tk_setPalette(background=self.PREFERRED_GRAY)
        self.master.config(menu=tk.Menu(self.master))

        # Used to validate data port entry widget values
        self.device_number_vcmd = (self.register(self.validate), '%d', '%P', DataPort.MIN_DEVICE_NUMBER, DataPort.MAX_DEVICE_NUMBER)
        self.channels_vcmd = (self.register(self.validate), '%d', '%P', DataPort.MIN_CHANNELS, DataPort.MAX_CHANNELS)
        self.channel_grouping_vcmd = (
            self.register(self.validate), '%d', '%P', DataPort.MIN_CHANNEL_GROUPING, DataPort.MAX_CHANNEL_GROUPING)
        self.channel_group_spacing_vcmd = (self.register(self.validate), '%d', '%P', DataPort.MIN_CHANNEL_GROUP_SPACING,
                                           DataPort.MAX_CHANNEL_GROUP_SPACING)
        self.sample_width_vcmd = (self.register(self.validate), '%d', '%P', DataPort.MIN_SAMPLE_WIDTH, DataPort.MAX_SAMPLE_WIDTH)
        self.sample_grouping_vcmd = (self.register(self.validate), '%d', '%P', DataPort.MIN_SAMPLE_GROUPING, DataPort.MAX_SAMPLE_GROUPING)
        self.interval_vcmd = (self.register(self.validate), '%d', '%P', DataPort.MIN_INTERVAL, DataPort.MAX_INTERVAL)
        self.numerator_vcmd = (self.register(self.validate), '%d', '%P', DataPort.MIN_SKIPPING_NUMERATOR, DataPort.MAX_SKIPPING_NUMERATOR)
        self.offset_vcmd = (self.register(self.validate), '%d', '%P', DataPort.MIN_OFFSET, DataPort.MAX_OFFSET)
        self.column_vcmd = (self.register(self.validate), '%d', '%P', 0, Interface.MAX_COLUMNS - 1)
        self.tail_width_vcmd = (self.register(self.validate), '%d', '%P', Interface.MIN_TAIL_WIDTH, Interface.MAX_TAIL_WIDTH)
        self.bit_width_vcmd = (self.register(self.validate), '%d', '%P', Interface.MIN_BIT_WIDTH, Interface.MAX_BIT_WIDTH)

        dp_entry_box_validate_functions = [self.device_number_vcmd,
                                           self.channels_vcmd,
                                           self.channel_grouping_vcmd,
                                           self.channel_group_spacing_vcmd,
                                           self.sample_width_vcmd,
                                           self.sample_grouping_vcmd,
                                           self.interval_vcmd,
                                           self.numerator_vcmd,
                                           self.offset_vcmd,
                                           self.column_vcmd,
                                           self.column_vcmd,
                                           self.tail_width_vcmd,
                                           self.bit_width_vcmd,
                                           self.column_vcmd]

        # Used to validate interface parameter entry widget values
        self.columns_vcmd = (self.register(self.validate), '%d', '%P', Interface.MIN_COLUMNS, Interface.MAX_COLUMNS)
        self.columns_per_row_vcmd = (self.register(self.validate), '%d', '%P', 1, Interface.MAX_COLUMNS_PER_ROW)
        self.rows_vcmd = (self.register(self.validate), '%d', '%P', self.MIN_ROWS_IN_FRAME, self.MAX_ROWS_IN_FRAME)
        self.denominator_vcmd = (
            self.register(self.validate), '%d', '%P', Interface.MIN_SKIPPING_DENOMINATOR, Interface.MAX_SKIPPING_DENOMINATOR)
        self.cds_s0_handover_width_vcmd = (self.register(self.validate), '%d', '%P', Interface.MIN_CDS_S0_HANDOVER_WIDTH, Interface.MAX_CDS_S0_HANDOVER_WIDTH)
        self.s0_width_vcmd = (self.register(self.validate), '%d', '%P', Interface.MIN_S0_WIDTH, Interface.MAX_S0_WIDTH)
        # self.s1_tails_vcmd = (self.register(self.validate), '%d', '%P', Interface.MIN_S1_TAILS, Interface.MAX_S1_TAILS)
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

        # We'll use these to detect bus clashes of various kinds
        self.bit_slots_source = []
        self.bit_slots_source_device = []
        self.bit_slots_source_clashed = []
        self.bit_slots_sink = []
        self.bit_slots_sink_device = []
        self.bit_slots_sink_clashed = []
        self.bit_slots_guard = []
        self.bit_slots_guard_device = []
        self.bit_slots_guard_clashed = []
        self.bit_slots_tail = []
        self.bit_slots_tail_device = []
        self.bit_slots_tail_clashed = []
        self.bit_slots_turnaround = []
        self.bit_slots_turnaround_device = []
        self.bit_slots_turnaround_clashed = []

        self.dp_enable_check_button_vars = []
        self.dp_manager_check_button_vars = []
        self.dp_direction_check_button_vars = []
        self.dp_ta_enable_check_button_vars = []
        self.dp_tail_enable_check_button_vars = []
        self.dp_guard_enable_check_button_vars = []
        self.dp_sri_enable_check_button_vars = []
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
       # self.dp_parameter_labels[-1].grid(row=count+2, column=0)

        # Data port entry widgets
        for entry_row in range(0, DataPort.NUM_DP_PARAMETERS):  # Columns
            for entry_column, data_port in enumerate(self.interface.data_ports):  # Rows
                self.dp_entry_boxes.append(self.create_entry(dp_entry_box_validate_functions[entry_row]))
                self.dp_entry_boxes[-1].grid(row=entry_row + 1, column=entry_column + 1, padx=3)
                self.dp_entry_boxes[-1].bind('<Return>', self.master_focus)

        # Data port direction checkbutton widgets
        self.dp_sample_rate_labels = []
        for count, data_port in enumerate(self.interface.data_ports):
            self.dp_direction_check_button_vars.append(tk.BooleanVar(value=data_port.source_REG))
            cb = tk.Checkbutton(self.config_frame, justify=tk.CENTER, variable=self.dp_direction_check_button_vars[count])
            cb.grid(row=DataPort.NUM_DP_PARAMETERS+1, column=count+1)

            self.dp_ta_enable_check_button_vars.append(tk.BooleanVar(value=data_port.handover))
            cb = tk.Checkbutton(self.config_frame, justify=tk.CENTER, variable=self.dp_ta_enable_check_button_vars[count])
            cb.grid(row=DataPort.NUM_DP_PARAMETERS+2, column=count+1)

            self.dp_guard_enable_check_button_vars.append(tk.BooleanVar(value=data_port.guard_REG))
            cb = tk.Checkbutton(self.config_frame, justify=tk.CENTER, variable=self.dp_guard_enable_check_button_vars[count])
            cb.grid(row=DataPort.NUM_DP_PARAMETERS+3, column=count+1)

            self.dp_sri_enable_check_button_vars.append(tk.BooleanVar(value=data_port.sri_REG))
            cb = tk.Checkbutton(self.config_frame, justify=tk.CENTER, variable=self.dp_sri_enable_check_button_vars[count])
            cb.grid(row=DataPort.NUM_DP_PARAMETERS+4, column=count+1)

            self.dp_enable_check_button_vars.append(tk.BooleanVar(value=data_port.enabled))
            cb = tk.Checkbutton(self.config_frame, justify=tk.CENTER, variable=self.dp_enable_check_button_vars[count])
            cb.grid(row=DataPort.NUM_DP_PARAMETERS+5, column=count+1)

            self.dp_manager_check_button_vars.append(tk.BooleanVar(value=data_port.inManager))
            cb = tk.Checkbutton(self.config_frame, justify=tk.CENTER, variable=self.dp_manager_check_button_vars[count])
            cb.grid(row=DataPort.NUM_DP_PARAMETERS+6, column=count+1)

            self.dp_sample_rate_labels.append(
                tk.Label(self.config_frame, text=data_port.sample_rate, anchor=tk.CENTER, font=(self.APP_FONT, self.TEXT_SIZE)))
            self.dp_sample_rate_labels[count].grid(row=DataPort.NUM_DP_PARAMETERS+7, column=count+1)

        # Interface parameter label widgets
        self.frame_labels = [
            tk.Label(self.config_frame, text='Data Port Parameters', anchor=tk.CENTER, font=(self.APP_FONT, self.TEXT_SIZE+6), padx=10)]
        self.frame_labels[-1].grid(row=0, column=0)

        # Interface Parameters
        self.frame_labels.append(
            tk.Label(self.config_frame, text='Miscellaneous Parameters', anchor=tk.CENTER, font=(self.APP_FONT, self.TEXT_SIZE+6), padx=10))
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

        # Columns per row
        self.cpr_entry = self.create_entry(self.columns_per_row_vcmd)
        self.cpr_entry.grid(row=1, column=Interface.NUM_DATA_PORTS+3)
        self.cpr_entry.bind('<Return>', self.master_focus)

        # S0/S1 enable
        cb = tk.Checkbutton(self.config_frame, justify=tk.CENTER, variable=self.s0s1_enabled_tk)
        cb.grid(row=2, column=Interface.NUM_DATA_PORTS+3)

        # S0 width
        self.s0w_entry = self.create_entry(self.s0_width_vcmd)
        self.s0w_entry.grid(row=3, column=Interface.NUM_DATA_PORTS+3)
        self.s0w_entry.bind('<Return>', self.master_focus)

        # Number of S1 Tails   TODO:  Should probably remove the hard coded row number here to add more parameters.
        #self.s1tails_entry = self.create_entry(self.s1_tails_vcmd)
        #self.s1tails_entry.grid( row = 4, column = Interface.NUM_DATA_PORTS + 3 )
        #self.s1tails_entry.bind('<Return>', self.master_focus)

        # Control Data Stream guard
        cb = tk.Checkbutton(self.config_frame, justify=tk.CENTER, variable=self.cd0_enable_tk)
        cb.grid(row=4, column=Interface.NUM_DATA_PORTS+3)

        # CDS Tail width
        self.tail_width_entry = self.create_entry(self.tail_width_vcmd)
        self.tail_width_entry.grid(row=5, column=Interface.NUM_DATA_PORTS+3)
        self.tail_width_entry.bind('<Return>', self.master_focus)

        # Fractional skipping denominator
        self.fid_entry = self.create_entry(self.denominator_vcmd)
        self.fid_entry.grid(row=6, column=Interface.NUM_DATA_PORTS+3)
        self.fid_entry.bind('<Return>', self.master_focus)

        # Handover width
        self.cds_s0_handover_width_entry = self.create_entry(self.cds_s0_handover_width_vcmd)
        self.cds_s0_handover_width_entry.grid(row=7, column=Interface.NUM_DATA_PORTS+3)
        self.cds_s0_handover_width_entry.bind('<Return>', self.master_focus)

        # Draw S0 handover
        cb = tk.Checkbutton(self.config_frame, justify=tk.CENTER, variable=self.s0_ta_enable_tk)
        cb.grid(row=8, column=Interface.NUM_DATA_PORTS+3)

        # Row rate
        self.row_rate_entry = self.create_entry(self.row_rate_vcmd)
        self.row_rate_entry.grid(row=9, column=Interface.NUM_DATA_PORTS + 3)
        self.row_rate_entry.bind('<Return>', self.master_focus)

        # Rows to draw
        self.rpf_entry = self.create_entry(self.rows_vcmd)
        self.rpf_entry.grid(row=10, column=Interface.NUM_DATA_PORTS+3)
        self.rpf_entry.bind('<Return>', self.master_focus)

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
        btn2.grid(column=Interface.NUM_DATA_PORTS + 2, columnspan=2, row=DataPort.NUM_DP_PARAMETERS + 2,
                  sticky=tk.N + tk.S + tk.E + tk.W, padx=10, pady=3)

        # Write configuration file button
        cmd3 = self.save_csv_file
        btn3 = tk.Button(self.config_frame, text='Save Configuration', default='active', command=cmd3,
                         font=(self.APP_FONT, self.TEXT_SIZE + 3), relief=tk.FLAT)
        btn3.grid(column=Interface.NUM_DATA_PORTS + 2, columnspan=2, row=DataPort.NUM_DP_PARAMETERS + 4,
                  sticky=tk.N + tk.S + tk.E + tk.W, padx=10, pady=3)

        # Redraw data ports button
        cmd1 = self.refresh_data_ports
        btn1 = tk.Button(self.config_frame, text='Redraw', default='active', command=cmd1,
                         font=(self.APP_FONT, self.TEXT_SIZE + 3), relief=tk.FLAT)
        btn1.grid(column=Interface.NUM_DATA_PORTS + 2, columnspan=2, row=DataPort.NUM_DP_PARAMETERS + 6,
                  sticky=tk.N + tk.S + tk.E + tk.W, padx=10, pady=3)

        if self.args.config_filename is not None:
            self.load_csv_file_int(self.args.config_filename)

        self.update_ui()
        self.refresh_data_ports()

        # if in batch mode exit here
        if (self.args.batch_mode or ( self.args.simple_mode and finished_drawing ) ):
            exit(0)


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
        filename = filedialog.asksaveasfilename(title="Select an output parameter file name", defaultextension=".csv",
                                                filetypes=[("CSV Files", "*.csv")])

        if filename:
            with io.open(filename, 'w', encoding='utf8') as outfile:
                writer = csv.writer(outfile, delimiter=',', lineterminator='\n')
                frame_values = [self.interface.columns_per_row,
                                self.interface.s0s1_enabled,
                                self.interface.s0_width,
                                #self.interface.s1_tails,
                                self.interface.cds_guard_enabled,
                                self.interface.tail_width,
                                self.interface.skipping_denominator_REG,
                                self.interface.cds_s0_handover_width,
                                self.interface.s0_handover_enabled,
                                self.interface.row_rate,
                                self.rows_in_frame]
                row = [ SAVE_CODING_STRING ] + [ str( SAVE_FILE_USING_EXCESS_ONE ) ]
                writer.writerow(row)                
                for count, value in enumerate(frame_values):
                    row = [self.INTERFACE_PARAMETER_TITLES[count]] + [str(value)]
                    writer.writerow(row)
                row = [ DATA_PORT_NAME ] + [data_port.name for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = [ DATA_PORT_DEVICE_NUMBER ] + [str(data_port.device_number) for data_port in self.interface.data_ports]
                writer.writerow(row)
                if ( SAVE_FILE_USING_EXCESS_ONE ) :
                    row = [ DATA_PORT_CHANNELS ] + [str(data_port.channels_REG) for data_port in self.interface.data_ports]
                else :
                    row = [ DATA_PORT_CHANNELS ] + [str(data_port.channels_REG + 1) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = [ DATA_PORT_CHANNEL_GROUPING ] + [str(data_port.channel_grouping_REG) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = [ DATA_PORT_CHANNEL_GROUP_SPACING ] + [str(data_port.channel_group_spacing_REG) for data_port in
                                                             self.interface.data_ports]
                writer.writerow(row)
                if ( SAVE_FILE_USING_EXCESS_ONE ) :
                    row = [ DATA_PORT_SAMPLE_WIDTH ] + [str(data_port.sample_width_REG) for data_port in self.interface.data_ports]
                else :
                    row = [ DATA_PORT_SAMPLE_WIDTH ] + [str(data_port.sample_width_REG + 1) for data_port in self.interface.data_ports]                    
                writer.writerow(row)
                if ( SAVE_FILE_USING_EXCESS_ONE ) :
                    row = [ DATA_PORT_SAMPLE_GROUPING ] + [str(data_port.sample_grouping_REG) for data_port in self.interface.data_ports]
                else :
                    row = [ DATA_PORT_SAMPLE_GROUPING ] + [str(data_port.sample_grouping_REG + 1) for data_port in self.interface.data_ports]
                writer.writerow(row)
                if ( SAVE_FILE_USING_EXCESS_ONE ) :
                    row = [ DATA_PORT_INTERVAL ] + [str(data_port.interval_REG) for data_port in self.interface.data_ports]
                else :
                    row = [ DATA_PORT_INTERVAL ] + [str(data_port.interval_REG + 1) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = [ DATA_PORT_SKIPPING_NUMERATOR ] + [str(data_port.skipping_numerator_REG) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = [ DATA_PORT_OFFSET ] + [str(data_port.offset_REG) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = [ DATA_PORT_HORIZONTAL_START ] + [str(data_port.horizontal_start_REG) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = [ DATA_PORT_HORIZONTAL_COUNT ] + [str(data_port.horizontal_count_REG) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = [ DATA_PORT_TAIL_WIDTH ] + [str(data_port.tail_width_REG) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = [ DATA_PORT_BIT_WIDTH ] + [str(data_port.bit_width_REG) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = [ DATA_PORT_IS_SOURCE ] + [str(data_port.source_REG) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = [ DATA_PORT_DRAW_HANDOVER ] + [str(data_port.handover) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = [ DATA_PORT_GUARD_ENABLED ] + [str(data_port.guard_REG) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = [ DATA_PORT_ENABLED ] + [str(data_port.enabled) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = [ DATA_PORT_IN_MANAGER ] + [str(data_port.inManager) for data_port in self.interface.data_ports]
                writer.writerow(row)
                row = [ DATA_PORT_SRI ] + [str(data_port.sri_REG) for data_port in self.interface.data_ports]
                writer.writerow(row)
            outfile.close()
            # Write SVG export file if the canvasvg module was found
            if canvasvg:
                canvasvg.saveall(os.path.splitext(filename)[0] + '.svg', self.render_canvas)
            else:
                messagebox.showwarning('Warning!', 'No canvasvg.')

    def save_frame_model(self, filename, model):
        if self.args.batch_mode :
            if Debug_FileIO : print( 'about to put result in', os.path.dirname(filename) )
            os.makedirs(os.path.dirname(filename), exist_ok=True)
            fh = openfile(filename, 'w')
            json.dump(model, fh, cls=SimpleJSONEncoder, indent=4)

    # Writes UI elements
    def update_ui(self):

        # Update data port names
        for count, x in enumerate(self.dp_name_entry_boxes):
            if Debug_Drawing : print( x )
            x.delete(0, tk.END)
            x.insert(0, self.interface.data_ports[count].name)

        # Data port entry widgets
        for entry_column, data_port in enumerate(self.interface.data_ports):  # Rows
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 0 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 0 + entry_column].insert(0,
                                                                                         self.interface.data_ports[
                                                                                             entry_column].device_number)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 1 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 1 + entry_column].insert(0,
                                                                                         self.interface.data_ports[entry_column].channels_REG + ( 0 if UI_IS_EXCESS_1 else 1 ) )
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 2 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 2 + entry_column].insert(0, self.interface.data_ports[
                entry_column].channel_grouping_REG)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 3 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 3 + entry_column].insert(0,
                                                                                         self.interface.data_ports[
                                                                                             entry_column].channel_group_spacing_REG)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 4 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 4 + entry_column].insert(0,
                                                                                         self.interface.data_ports[
                                                                                             entry_column].sample_width_REG + ( 0 if UI_IS_EXCESS_1 else 1 ) )
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 5 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 5 + entry_column].insert(0,
                                                                                         self.interface.data_ports[
                                                                                             entry_column].sample_grouping_REG + ( 0 if UI_IS_EXCESS_1 else 1 ) )
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 6 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 6 + entry_column].insert(0,
                                                                                         self.interface.data_ports[
                                                                                             entry_column].interval_REG + ( 0 if UI_IS_EXCESS_1 else 1 ) )
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 7 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 7 + entry_column].insert(0,
                                                                                         self.interface.data_ports[
                                                                                             entry_column].skipping_numerator_REG)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 8 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 8 + entry_column].insert(0,
                                                                                         self.interface.data_ports[entry_column].offset_REG)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 9 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 9 + entry_column].insert(0,
                                                                                         self.interface.data_ports[entry_column].horizontal_start_REG)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 10 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 10 + entry_column].insert(0,
                                                                                         self.interface.data_ports[entry_column].horizontal_count_REG)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 11 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 11 + entry_column].insert(0,
                                                                                         self.interface.data_ports[entry_column].tail_width_REG)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 12 + entry_column].delete(0, tk.END)
            self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 12 + entry_column].insert(0,
                                                                                          self.interface.data_ports[
                                                                                              entry_column].bit_width_REG)

        # Data port direction
        for count, x in enumerate(self.dp_direction_check_button_vars):
            x.set(self.interface.data_ports[count].source_REG)

        # Data port turn around enables
        for count, x in enumerate(self.dp_ta_enable_check_button_vars):
            x.set(self.interface.data_ports[count].handover)

        # Data port zero enables
        for count, x in enumerate(self.dp_guard_enable_check_button_vars):
            x.set(self.interface.data_ports[count].guard_REG)

        # Data port zero enables
        for count, x in enumerate(self.dp_sri_enable_check_button_vars):
            x.set(self.interface.data_ports[count].sri_REG)

        # Data port enables
        for count, x in enumerate(self.dp_enable_check_button_vars):
            x.set(self.interface.data_ports[count].enabled)

        # Data port in Manager
        for count, x in enumerate(self.dp_manager_check_button_vars):
            x.set(self.interface.data_ports[count].inManager)

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
        # S1 tails
        #self.s1tails_entry.delete(0, tk.END)
        #self.s1tails_entry.insert(tk.END, self.interface.s1_tails)
        # S0 TA enable
        self.s0_ta_enable_tk.set(self.interface.s0_handover_enabled)
        # CDS guard enable
        self.cd0_enable_tk.set(self.interface.cds_guard_enabled)
        # CDS S0 Handover width
        self.cds_s0_handover_width_entry.delete(0, tk.END)
        self.cds_s0_handover_width_entry.insert(tk.END, self.interface.cds_s0_handover_width)
        # Tail width
        self.tail_width_entry.delete(0, tk.END)
        self.tail_width_entry.insert(tk.END, self.interface.tail_width)
        # Fractional skipping denominator
        self.fid_entry.delete(0, tk.END)
        self.fid_entry.insert(tk.END, self.interface.skipping_denominator_REG)
        # Row rate
        self.row_rate_entry.delete(0, tk.END)
        self.row_rate_entry.insert(tk.END, self.interface.row_rate)

        # Update sample rate labels
        for count, x in enumerate(self.dp_sample_rate_labels):
#            if self.interface.data_ports[count].interval_REG != 0:
                if not self.interface.data_ports[count].sri_REG and ( 0 == self.interface.data_ports[count].skipping_numerator_REG ) :
                    sample_rate = "{:.2f}".format( ( ( self.interface.data_ports[ count ].sample_grouping_REG + 1 ) * self.interface.row_rate /(  self.interface.data_ports[ count ].interval_REG + 1 ) ) )
                elif not self.interface.data_ports[count].sri_REG :                           
                    sample_rate = "{:.2f}".format( ( ( self.interface.data_ports[ count ].sample_grouping_REG + 1 ) * self.interface.row_rate /( self.interface.data_ports[ count ].interval_REG + 1 ) ) *
                                                   ( (self.interface.skipping_denominator_REG - self.interface.data_ports[count].skipping_numerator_REG ) /
                                                     ( self.interface.skipping_denominator_REG ) ) )
                else : # it is SRI
                    sample_rate = "{:.2f}".format( ( ( self.interface.data_ports[ count ].sample_grouping_REG + 1 ) * self.interface.row_rate *
                                                     math.ceil( ( self.interface.data_ports[count].horizontal_count_REG + 1 ) / ( ( self.interface.data_ports[count].channels_REG + 1 ) * ( self.interface.data_ports[count].sample_grouping_REG + 1 )* ( self.interface.data_ports[count].sample_width_REG + 1 ) + ( self.interface.data_ports[count].channel_group_spacing_REG - 1 ) ) ) ) )
                x.config( text = sample_rate )
#            else:
#                x.config( text = 'Error' )

        # Update Interval LCM
        self.frame_labels[-1].config(text=self.interface.interval_lcm)

    # Reads UI elements
    def update_model(self):

        # Update data port names
        for count, entry_box in enumerate(self.dp_name_entry_boxes):
            self.interface.data_ports[count].name = entry_box.get()

        # Data port entry widgets
        for entry_column, data_port in enumerate(self.interface.data_ports):  # Rows
            self.interface.data_ports[entry_column].device_number = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 0 + entry_column].get())

            self.interface.data_ports[entry_column].channels_REG = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 1 + entry_column].get()) - ( 0 if UI_IS_EXCESS_1 else 1 )
            self.interface.data_ports[entry_column].channel_grouping_REG = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 2 + entry_column].get())

            self.interface.data_ports[entry_column].channel_group_spacing_REG = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 3 + entry_column].get())

            self.interface.data_ports[entry_column].sample_width_REG = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 4 + entry_column].get()) - ( 0 if UI_IS_EXCESS_1 else 1 )

            self.interface.data_ports[entry_column].sample_grouping_REG = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 5 + entry_column].get()) - ( 0 if UI_IS_EXCESS_1 else 1 )

            self.interface.data_ports[entry_column].interval_REG = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 6 + entry_column].get()) - ( 0 if UI_IS_EXCESS_1 else 1 )

            self.interface.data_ports[entry_column].skipping_numerator_REG = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 7 + entry_column].get())

            self.interface.data_ports[entry_column].offset_REG = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 8 + entry_column].get())

            self.interface.data_ports[entry_column].horizontal_start_REG = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 9 + entry_column].get())

            self.interface.data_ports[entry_column].horizontal_count_REG = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 10 + entry_column].get())

            self.interface.data_ports[entry_column].tail_width_REG = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 11 + entry_column].get())

            self.interface.data_ports[entry_column].bit_width_REG = \
                self.st_int(self.dp_entry_boxes[self.interface.NUM_DATA_PORTS * 12 + entry_column].get())

        # read & error check rows per frame
        self.rows_in_frame = max(self.MIN_ROWS_IN_FRAME,
                                 min(self.MAX_ROWS_IN_FRAME,
                                     self.st_int(self.rpf_entry.get())))

        # read & error check columns per row
        self.interface.columns_per_row = max(Interface.MIN_COLUMNS_PER_ROW,
                                             min(Interface.MAX_COLUMNS_PER_ROW,
                                                 self.st_int(self.cpr_entry.get())))
        # If odd, make even
        self.interface.columns_per_row -= \
            self.interface.columns_per_row % 2

        self.interface.s0s1_enabled = self.s0s1_enabled_tk.get()

        # read & error check s0 width
        self.interface.s0_width = max(Interface.MIN_S0_WIDTH,
                                      min(Interface.MAX_S0_WIDTH,
                                          self.st_int(self.s0w_entry.get())))

        #self.interface.s1_tails = max(Interface.MIN_S1_TAILS, min(Interface.MAX_S1_TAILS, self.st_int(self.s1tails_entry.get())))

        self.interface.s0_handover_enabled = self.s0_ta_enable_tk.get()

        self.interface.cds_guard_enabled = self.cd0_enable_tk.get()

        # read & error check Handover width
        self.interface.cds_s0_handover_width = max(Interface.MIN_CDS_S0_HANDOVER_WIDTH,
                                            min(Interface.MAX_CDS_S0_HANDOVER_WIDTH, self.st_int(self.cds_s0_handover_width_entry.get())))

        # read & error check Tail width
        self.interface.tail_width = max(Interface.MIN_TAIL_WIDTH, min(Interface.MAX_TAIL_WIDTH, self.st_int(self.tail_width_entry.get())))

        # read & error check fractional skipping denominator
        self.interface.skipping_denominator_REG = max(Interface.MIN_SKIPPING_DENOMINATOR,
                                                  min(Interface.MAX_SKIPPING_DENOMINATOR, self.st_int(self.fid_entry.get())))

        # read & error check row rate
        # self.interface.row_rate = max(1, min(self.interface.row_rate - 1, self.st_int(self.interface.row_rate_entry.get())))
        self.interface.row_rate = self.st_int(self.row_rate_entry.get())

        # Data port direction check button widgets
        for count, direction in enumerate(self.dp_direction_check_button_vars):
            self.interface.data_ports[count].source_REG = bool(direction.get())

        # Data port handover enable check button widgets
        for count, x in enumerate(self.dp_ta_enable_check_button_vars):
            self.interface.data_ports[count].handover = bool(x.get())

        # Data port guard enable check button widgets
        for count, x in enumerate(self.dp_guard_enable_check_button_vars):
            self.interface.data_ports[count].guard_REG = bool(x.get())

        # Data port sri enable check button widgets
        for count, x in enumerate(self.dp_sri_enable_check_button_vars):
            self.interface.data_ports[count].sri_REG = bool(x.get())

        # Data port enable check button widgets
        for count, x in enumerate(self.dp_enable_check_button_vars):
            self.interface.data_ports[count].enabled = bool(x.get())

        # Data in Manager check button widgets
        for count, x in enumerate(self.dp_manager_check_button_vars):
            self.interface.data_ports[count].inManager = bool(x.get())

    # Draws all data ports
    def refresh_data_ports(self):
        self.master_focus()
        # Clear the canvas
        self.render_canvas.delete(tk.ALL)
        self.header_canvas.delete(tk.ALL)

        self.bit_slots_source[:] = []
        self.bit_slots_source_device[:] = []
        self.bit_slots_source_clashed[:] = []
        self.bit_slots_sink[:] = []
        self.bit_slots_sink_device[:] = []
        self.bit_slots_sink_clashed[:] = []
        self.bit_slots_guard[:] = []
        self.bit_slots_guard_device[:] = []
        self.bit_slots_guard_clashed[:] = []
        self.bit_slots_tail[:] = []
        self.bit_slots_tail_device[:] = []
        self.bit_slots_tail_clashed[:] = []
        self.bit_slots_turnaround[:] = []
        self.bit_slots_turnaround_device[:] = []
        self.bit_slots_turnaround_clashed[:] = []

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

        self.frame_model = Frame_model(self.rows_in_frame, self.interface.columns_per_row)

        # S0 S1 Columns
        if self.interface.s0s1_enabled:
            if self.interface.columns_per_row > self.interface.s0_width + 3 + int(self.interface.cds_guard_enabled) + int(
                    self.interface.s0_handover_enabled) * self.interface.cds_s0_handover_width:
                # S0 Column(s)
                if self.interface.s0_handover_enabled:
                    for column_offset in range(0, self.interface.cds_s0_handover_width):
                        # self.draw_column(self.interface.columns_per_row - column_offset - self.interface.s0_width - 1, 0,  'TA')
                        pass

                for column_offset in range(0, self.interface.s0_width):
                    self.draw_column(self.interface.columns_per_row - column_offset - 1, 0, 'S0')

                self.draw_column(0, 0, 'S1')

                for column_offset in range(0, self.interface.cds_s0_handover_width):
                    self.draw_column(1 + column_offset + self.interface.tail_width, 0, 'TA')

                self.draw_column(1 + self.interface.cds_s0_handover_width + self.interface.tail_width, 0, 'CDS')

                if self.interface.cds_guard_enabled:
                    self.draw_column(2 + self.interface.cds_s0_handover_width + self.interface.tail_width, 0, GuardText)

                for column_offset in range(0, self.interface.cds_s0_handover_width):
                    self.draw_column(2 + self.interface.cds_s0_handover_width + 2 * self.interface.tail_width + int(
                        self.interface.cds_guard_enabled) + column_offset, 0, 'TA')

                # TODO: can't this be done by draw_column?
                # If so, we could let draw_column update frame_model in all the cases
                # under if self.interface.s0s1_enabled: else: ...
                for column_offset in range(0, self.interface.tail_width):
                    for count in range(0, self.rows_in_frame):
                        self.draw_tail(count, 1 + column_offset, 0, self.PREFERRED_GRAY)
                        self.draw_tail(count, 2 + self.interface.tail_width + self.interface.cds_s0_handover_width + int(
                            self.interface.cds_guard_enabled) + column_offset,
                                       0, self.PREFERRED_GRAY)
            else:
                self.master.update()
                messagebox.showwarning('Error!', 'Unable to draw S0, S1 & control stream columns: Too few columns in each row.')
        else:
            self.draw_column(0, 0, 'CDS')
            if self.interface.cds_guard_enabled:
                self.draw_column(1, 0, GuardText)
            for column_offset in range(0, self.interface.cds_s0_handover_width):
                self.draw_column(int(self.interface.cds_guard_enabled) + column_offset + 1, 0, 'TA')
                self.draw_column(self.interface.columns_per_row - column_offset - 1, 0, 'TA')

        error_text = ''

        # Draw our data ports
        # Draw for each device in sequence
        # Loop though all devices
        if Debug_Drawing : print( "about to draw the data ports" )
        for device in range(0, 8):
            for count, data_port in enumerate(self.interface.data_ports):
                if data_port.device_number == device:
                    if data_port.enabled:
                        temp_text = self.draw_data_port(self.interface.data_ports[count], self.DP_COLORS[count])
                        # temp_text += self.draw_data_port_guard_tail(self.interface.data_ports[count], self.DP_COLORS[count])
                        if len(temp_text):
                            error_text += temp_text + 'In ' + self.interface.data_ports[count].name + '\n'
        self.master.update()

        if Debug_FileIO : print( "about to print filename: " )
        if Debug_FileIO : print( self.args.out_frame_filename )
        if (self.args.out_frame_filename is not None):
            if Debug_FileIO : print( 'about to save in', self.args.out_frame_filename )
            self.save_frame_model(self.args.out_frame_filename, self.frame_model)

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

        filename = filedialog.askopenfilename(title="Select an input parameter filename",
                                              filetypes=[("CSV Files", "*.csv")])

        self.load_csv_file_int(filename)        

    def load_csv_file_int(self, filename):

        if filename:
            with open(filename) as data_file:
                csv_data = csv.reader(data_file)
                file_is_excess_one = False
                for count, row in enumerate(csv_data):
                    if Debug_FileIO : print( "when reading number", count, "we see '", row[0], " and ", row[1] )
                    # TODO: Technical debt:  Rewrite the following to not use strings that are in the xxx struct 
                    if 0 == row[0].find( "Columns per Row", 0 ) :
                        self.interface.columns_per_row = self.st_int(row[ 1 ] )
                    elif 0 == row[0].find( "S0 S1 Enabled", 0 ) :
                        self.interface.s0s1_enabled = bool( distutils.util.strtobool( row[ 1 ] ) )
                    elif 0 == row[0].find( "S0 Width", 0 ) :
                        self.interface.s0_width = self.st_int( row[ 1 ] )
                    elif 0 == row[0].find( "CDS Guard Enabled", 0 ) :
                        self.interface.cds_guard_enabled = bool( distutils.util.strtobool( row[ 1 ] ) )
                    elif 0 == row[0].find( "CDS Tail Width", 0 ) :
                        self.interface.tail_width = self.st_int( row[ 1 ] )
                    elif 0 == row[0].find( "Interval Denominator", 0 ) :
                        self.interface.skipping_denominator_REG = self.st_int( row[ 1 ] )
                    elif 0 == row[0].find( "CDS/S0 Handover Width", 0 ) :
                        self.interface.cds_s0_handover_width = self.st_int( row[1])
                    elif 0 == row[0].find( "Draw S0 Hanbdover", 0 ):
                        self.interface.s0_handover_enabled = bool( distutils.util.strtobool( row[ 1 ] ) )
                    elif 0 == row[0].find( "Row Rate", 0 ):
                        self.interface.row_rate = self.st_int( row[ 1 ] )
                    elif 0 == row[0].find( "Rows to Draw", 0 ):
                        self.rows_in_frame = self.st_int( row[ 1 ] )
                    elif SAVE_CODING_STRING == row[ 0 ]:
                        file_is_excess_one = bool( distutils.util.strtobool( row[ 1 ] ) )
                    elif DATA_PORT_NAME == row[ 0 ] :                    
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.name = row[obj_count + 1]
                    elif DATA_PORT_DEVICE_NUMBER == row[ 0 ] :
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.device_number = self.st_int(row[obj_count + 1])
                    elif DATA_PORT_CHANNELS == row[ 0 ] :
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            if not file_is_excess_one :
                                data_port.channels_REG = self.st_int(row[obj_count + 1]) - 1
                            else :
                                data_port.channels_REG = self.st_int(row[obj_count + 1])
                    elif DATA_PORT_CHANNEL_GROUPING == row[ 0 ] :
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.channel_grouping_REG = self.st_int(row[obj_count + 1])
                    elif DATA_PORT_CHANNEL_GROUP_SPACING == row[ 0 ] :
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.channel_group_spacing_REG = self.st_int(row[obj_count + 1])
                    elif DATA_PORT_SAMPLE_WIDTH == row[ 0 ] :
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            if not file_is_excess_one :
                                data_port.sample_width_REG = self.st_int(row[obj_count + 1]) - 1
                            else :
                                data_port.sample_width_REG = self.st_int(row[obj_count + 1])
                    elif DATA_PORT_SAMPLE_GROUPING == row[ 0 ] :
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            if not file_is_excess_one :
                                data_port.sample_grouping_REG = self.st_int(row[obj_count + 1]) - 1
                            else :
                                data_port.sample_grouping_REG = self.st_int(row[obj_count + 1])
                    elif DATA_PORT_INTERVAL == row[ 0 ]:
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            if not file_is_excess_one :
                                data_port.interval_REG = self.st_int(row[obj_count + 1]) - 1
                            else :
                                data_port.interval_REG = self.st_int(row[obj_count + 1])
                    elif DATA_PORT_SKIPPING_NUMERATOR == row[ 0 ] :
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.skipping_numerator_REG = self.st_int(row[obj_count + 1])
                    elif DATA_PORT_OFFSET == row[ 0 ] :
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.offset_REG = self.st_int(row[obj_count + 1])
                    elif DATA_PORT_HORIZONTAL_START == row[ 0 ] :
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.horizontal_start_REG = self.st_int(row[obj_count + 1])
                    elif DATA_PORT_HORIZONTAL_COUNT == row[ 0 ] :
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.horizontal_count_REG = self.st_int(row[obj_count + 1])
                    elif DATA_PORT_TAIL_WIDTH == row[ 0 ] :
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.tail_width_REG = self.st_int(row[obj_count + 1])
                    elif DATA_PORT_BIT_WIDTH == row[ 0 ] :
                        for obj_count, data_port in enumerate(self.interface.data_ports):
                            data_port.bit_width_REG = self.st_int(row[obj_count + 1])
                    elif DATA_PORT_IS_SOURCE == row[ 0 ] :
                        if len(row[1:]) == Interface.NUM_DATA_PORTS:
                            for obj_count, data_port in enumerate(self.interface.data_ports):
                                data_port.source_REG = bool(distutils.util.strtobool(row[obj_count + 1]))
                        else:
                            messagebox.showwarning("Warning", "Error reading CSV file.\n" + str(
                                Interface.NUM_DATA_PORTS) + ' data port entry columns expected in row ' + str(
                                count + 1) + '.\nFound ' + str(len(row[1:])) + '.')
                            return
                    elif DATA_PORT_DRAW_HANDOVER == row[ 0 ] :
                        if len(row[1:]) == Interface.NUM_DATA_PORTS:
                            for obj_count, data_port in enumerate(self.interface.data_ports):
                                data_port.handover = bool(distutils.util.strtobool(row[obj_count + 1]))
                        else:
                            messagebox.showwarning("Warning", "Error reading CSV file.\n" + str(
                                Interface.NUM_DATA_PORTS) + ' data port entry columns expected in row ' + str(
                                count + 1) + '.\nFound ' + str(len(row[1:])) + '.')
                            return
                    elif DATA_PORT_GUARD_ENABLED == row[ 0 ] :
                        if len(row[1:]) == Interface.NUM_DATA_PORTS:
                            for obj_count, data_port in enumerate(self.interface.data_ports):
                                data_port.guard_REG = bool(distutils.util.strtobool(row[obj_count + 1]))
                        else:
                            messagebox.showwarning("Warning", "Error reading CSV file.\n" + str(
                                Interface.NUM_DATA_PORTS) + ' data port entry columns expected in row ' + str(
                                count + 1) + '.\nFound ' + str(len(row[1:])) + '.')
                            return
                    elif DATA_PORT_ENABLED == row[ 0 ] :
                        if len(row[1:]) == Interface.NUM_DATA_PORTS:
                            for obj_count, data_port in enumerate(self.interface.data_ports):
                                data_port.enabled = bool(distutils.util.strtobool(row[obj_count + 1]))
                        else:
                            messagebox.showwarning("Warning", "Error reading CSV file.\n" + str(
                                Interface.NUM_DATA_PORTS) + ' data port entry columns expected in row ' + str(
                                count + 1) + '.\nFound ' + str(len(row[1:])) + '.')
                            return
                    elif DATA_PORT_IN_MANAGER == row[ 0 ] :
                        if len(row[1:]) == Interface.NUM_DATA_PORTS:
                            for obj_count, data_port in enumerate(self.interface.data_ports):
                                data_port.inManager = bool(distutils.util.strtobool(row[obj_count + 1]))
                        else:
                            messagebox.showwarning("Warning", "Error reading CSV file.\n" + str(
                                Interface.NUM_DATA_PORTS) + ' data port entry columns expected in row ' + str(
                                count + 1) + '.\nFound ' + str(len(row[1:])) + '.')
                            return
                    elif DATA_PORT_SRI == row[0] :
                        if len(row[1:]) == Interface.NUM_DATA_PORTS:
                            for obj_count, data_port in enumerate(self.interface.data_ports):
                                data_port.sri_REG = bool(distutils.util.strtobool(row[obj_count + 1]))
                        else:
                            messagebox.showwarning("Warning", "Error reading CSV file.\n" + str(
                                Interface.NUM_DATA_PORTS) + ' data port entry columns expected in row ' + str(
                                count + 1) + '.\nFound ' + str(len(row[1:])) + '.')
                    #elif count == 28:
                    #    self.interface.s1_tails = self.st_int(row[1])

            data_file.close()
            self.master.update()
            self.update_ui()
            self.refresh_data_ports()
            finished_drawing = True

    # Draws a single repeating column in a frame
    def draw_column(self, column, device, text):
        if not isinstance(column, int):
            raise TypeError('Expected int for column')
        if not isinstance(text, str):
            raise TypeError('Expected str for text')
        for row in range(0, self.rows_in_frame):
            if text == 'TA':
                self.draw_handover(row, column, device)
            else:
                self.render_canvas.create_text((column + 2) * self.COLUMN_SIZE, (row + 2.5) * self.ROW_SIZE, text=text,
                                               font=(self.APP_FONT, self.TEXT_SIZE))
                self.check_bus_clash(row, column, device, 'write')

        # Frame model update
        for row in range(0, self.rows_in_frame):
            self.update_col_in_frame_model(row, column, 0, 0, 0, text, 0)

    # Draws a handover bit slot
    def draw_handover(self, row, column, device):
        if not isinstance(row, int):
            raise TypeError('Expected int for row')
        if not isinstance(column, int):
            raise TypeError('Expected int for column')
        if Debug_Drawing : print( "draw_handover called for column:" )
        if Debug_Drawing : print( column )
        if Debug_Drawing : print( " row:" )
        if Debug_Drawing : print( row )
        if Debug_Drawing : print( " device:" )
        if Debug_Drawing : print( device )
        self.render_canvas.create_line((column + 1.725) * self.COLUMN_SIZE, (row + 2.35) * self.ROW_SIZE,
                                       (column + 2.275) * self.COLUMN_SIZE,
                                       (row + 2.35) * self.ROW_SIZE, arrow=tk.LAST)
        self.render_canvas.create_line((column + 1.725) * self.COLUMN_SIZE, (row + 2.65) * self.ROW_SIZE,
                                       (column + 2.275) * self.COLUMN_SIZE,
                                       (row + 2.65) * self.ROW_SIZE, arrow=tk.FIRST)
        self.check_bus_clash(row, column, device, 'handover')

    # Draws a tail bit slot
    def draw_tail(self, row, column, device, color):
        if Debug_Drawing : print( 'draw_tail called with row={:d}, column={:d}, device={}'.format( row, column, device) )
        if not isinstance(row, int):
            raise TypeError('Expected int for row')
        if not isinstance(column, int):
            raise TypeError('Expected int for column')
        if not isinstance(color, str):
            raise TypeError('Expected str for color')
        direction = 1
        if self.check_bus_clash(row, column, device, 'tail'):
            self.render_canvas.create_rectangle((column + 1.5) * self.COLUMN_SIZE,
                                                (row + 2 + 0.5 * (direction == 0)) * self.ROW_SIZE + 2 * (
                                                        direction == 1) + 0 * (direction == 0),
                                                (column + 2.5) * self.COLUMN_SIZE,
                                                (row + 2.5 + 0.5 * (direction == 0)) * self.ROW_SIZE + 0 * (
                                                        direction == 1) - 1 * (direction == 0),
                                                fill=color, width=1)
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

    # Draws a guard bit slot
    def draw_guard(self, row, column, device, color):
        if Debug_Drawing : print( 'draw_guard called with row={:d}, column={:d}, device={}'.format( row, column, device) )
        if not isinstance(row, int):
            raise TypeError('Expected int for row')
        if not isinstance(column, int):
            raise TypeError('Expected int for column')
        if not isinstance(color, str):
            raise TypeError('Expected str for color')
        direction = 1
        if self.check_bus_clash(row, column, device, 'guard'):
            if Debug_Clash : print( "clash check returned True")
            self.render_canvas.create_rectangle((column + 1.5) * self.COLUMN_SIZE,
                                            (row + 2 + 0.5 * (direction == 0)) * self.ROW_SIZE + 2 * (
                                                    direction == 1) + 0 * (direction == 0),
                                            (column + 2.5) * self.COLUMN_SIZE,
                                            (row + 2.5 + 0.5 * (direction == 0)) * self.ROW_SIZE + 0 * (
                                                    direction == 1) - 1 * (direction == 0),
                                            fill=color, width=1)
            self.render_canvas.create_text((column + 2) * self.COLUMN_SIZE,
                                       (row + 2.25) * self.ROW_SIZE,
                                       font=(self.APP_FONT, self.TEXT_SIZE - 2),
                                       justify=tk.CENTER,
                                       text=GuardText)
        else :
            if Debug_Clash : print( "clash check returned False")


    def check_bus_clash(self, row, column, device, bit_slot_type):
        if not isinstance(row, int):
            raise TypeError('Expected int for row')
        if not isinstance(column, int):
            raise TypeError('Expected int for column')
        if not isinstance(bit_slot_type, str):
            raise TypeError('Expected str for bit_slot_type')

        bit_slot = column + row * self.interface.columns_per_row
        return_value = 0

        # write
        if bit_slot_type == 'write':
            # Check if this bit slot is already driven
            if bit_slot in self.bit_slots_source or \
                    (bit_slot in self.bit_slots_turnaround ) or \
                     (bit_slot in self.bit_slots_guard and device != self.bit_slots_guard_device[self.bit_slots_guard.index(bit_slot)]) or \
                      (bit_slot in self.bit_slots_tail and device != self.bit_slots_tail_device[self.bit_slots_tail.index(bit_slot)]):
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
                self.bit_slots_source_device.append(device)

        # guard
        if bit_slot_type == 'guard':
            # Check if this bit slot is already driven
            if bit_slot in self.bit_slots_source and device != self.bit_slots_source_device[self.bit_slots_source.index(bit_slot)]:
                # bus clash
                if bit_slot not in self.bit_slots_source_clashed:
                    self.bit_slots_source_clashed.append(bit_slot)
                self.render_canvas.create_rectangle((column + 1.5) * self.COLUMN_SIZE,
                                                    (row + 2) * self.ROW_SIZE + 2,
                                                    (column + 2.5) * self.COLUMN_SIZE,
                                                    (row + 2.5) * self.ROW_SIZE,
                                                    fill='black', width=0)
            elif bit_slot in self.bit_slots_source and device == self.bit_slots_source_device[self.bit_slots_source.index(bit_slot)]:
                # Same device already has a bit slot driven here
                return_value = 0
            elif bit_slot in self.bit_slots_guard and device != self.bit_slots_guard_device[self.bit_slots_guard.index(bit_slot)]:
                # bus clash
                if bit_slot not in self.bit_slots_source_clashed:
                    self.bit_slots_source_clashed.append(bit_slot)
                self.render_canvas.create_rectangle((column + 1.5) * self.COLUMN_SIZE,
                                                    (row + 2) * self.ROW_SIZE + 2,
                                                    (column + 2.5) * self.COLUMN_SIZE,
                                                    (row + 2.5) * self.ROW_SIZE,
                                                    fill='black', width=0)
            elif bit_slot in self.bit_slots_guard and device == self.bit_slots_guard_device[self.bit_slots_guard.index(bit_slot)]:
                # No action needed since the same device is requesting a guard in this location
                return_value = 0
            elif bit_slot in self.bit_slots_tail and device != self.bit_slots_tail_device[self.bit_slots_tail.index(bit_slot)]:
                # bus clash
                if bit_slot not in self.bit_slots_source_clashed:
                    self.bit_slots_source_clashed.append(bit_slot)
                self.render_canvas.create_rectangle((column + 1.5) * self.COLUMN_SIZE,
                                                    (row + 2) * self.ROW_SIZE + 2,
                                                    (column + 2.5) * self.COLUMN_SIZE,
                                                    (row + 2.5) * self.ROW_SIZE,
                                                    fill='black', width=0)
            elif bit_slot in self.bit_slots_tail and device == self.bit_slots_tail_device[self.bit_slots_tail.index(bit_slot)]:
                # Guard should have priority over tails within a device
                self.bit_slots_guard.append(bit_slot)
                self.bit_slots_guard_device.append(device)
                return_value = 1
            else:
                # No clash
                self.bit_slots_guard.append(bit_slot)
                self.bit_slots_guard_device.append(device)
                return_value = 1
        # tail
        if bit_slot_type == 'tail':
            # Check if this bit slot is already driven

            if bit_slot in self.bit_slots_source and device != self.bit_slots_source_device[self.bit_slots_source.index(bit_slot)]:
                # bus clash
                if bit_slot not in self.bit_slots_source_clashed:
                    self.bit_slots_source_clashed.append(bit_slot)
                self.render_canvas.create_rectangle((column + 1.5) * self.COLUMN_SIZE,
                                                    (row + 2) * self.ROW_SIZE + 2,
                                                    (column + 2.5) * self.COLUMN_SIZE,
                                                    (row + 2.5) * self.ROW_SIZE,
                                                    fill='black', width=0)
            elif bit_slot in self.bit_slots_source and device == self.bit_slots_source_device[self.bit_slots_source.index(bit_slot)]:
                # Same device already has a bit slot driven here
                return_value = 0
            elif bit_slot in self.bit_slots_tail and device != self.bit_slots_tail_device[self.bit_slots_tail.index(bit_slot)]:
                # bus clash
                if bit_slot not in self.bit_slots_source_clashed:
                    self.bit_slots_source_clashed.append(bit_slot)
                self.render_canvas.create_rectangle((column + 1.5) * self.COLUMN_SIZE,
                                                    (row + 2) * self.ROW_SIZE + 2,
                                                    (column + 2.5) * self.COLUMN_SIZE,
                                                    (row + 2.5) * self.ROW_SIZE,
                                                    fill='black', width=0)
            elif bit_slot in self.bit_slots_tail and device == self.bit_slots_tail_device[self.bit_slots_tail.index(bit_slot)]:
                # No action needed since the same device is requesting only a tail in this location
                return_value = 0
            elif bit_slot in self.bit_slots_guard and device != self.bit_slots_guard_device[self.bit_slots_guard.index(bit_slot)]:
                # bus clash
                if bit_slot not in self.bit_slots_source_clashed:
                    self.bit_slots_source_clashed.append(bit_slot)
                self.render_canvas.create_rectangle((column + 1.5) * self.COLUMN_SIZE,
                                                    (row + 2) * self.ROW_SIZE + 2,
                                                    (column + 2.5) * self.COLUMN_SIZE,
                                                    (row + 2.5) * self.ROW_SIZE,
                                                    fill='black', width=0)
            elif bit_slot in self.bit_slots_guard and device == self.bit_slots_guard_device[self.bit_slots_guard.index(bit_slot)]:
                # No action needed since the same device is requesting only a guard in this location
                return_value = 0
            else:
                # No clash
                self.bit_slots_tail.append(bit_slot)
                self.bit_slots_tail_device.append(device)
                return_value = 1

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
                self.bit_slots_sink_device.append(device)

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
        return return_value

    def update_col_in_frame_model(self, row, column, dp_num, is_source, width, rrrr, sample_num_in_group ):
        ri = self.frame_model.get_row(row)
        si = Slot_info()

        if "tail" == rrrr:
            si.slot_type = Slot_type.TAIL
            # TODO: is dp_num relevant in case of TAIL?
        elif 'G' == rrrr :
            si.slot_type = Slot_type.GUARD
            si.dp_num = dp_num
        elif 'HANDOVER' == rrrr :
            si.slot_type = Slot_type.HANDOVER
            si.dp_num = dp_num # TODO: is dp_num relevant in case of HANDOVER?
        elif 'TA' == rrrr:
            si.slot_type = Slot_type.HANDOVER
        elif 'CDS' == rrrr:
            si.slot_type = Slot_type.CDS
        elif 'S0' == rrrr:
            si.slot_type = Slot_type.S0
        elif 'S1' == rrrr:
            si.slot_type = Slot_type.S1

        elif NOT_OWNED != rrrr :
            si.slot_type = Slot_type.NORMAL
            if is_source:
                si.dir = Direction_type.SOURCE
            else:
                si.dir = Direction_type.SINK
            si.dp_num = dp_num
            chan = 0
            bit_num = 0
            m = re.match("^c(\d+)b(-?\d+)$", rrrr)
            if (m):
                chan     = m.group(1)
                bit_num  = m.group(2)
            else:
                if Debug_Drawing : print(f'Error: update_col_in_frame_model called with rrrr = {rrrr}')
                exit(1)
            si.channel = chan
            si.bit_num = bit_num
            si.sample = sample_num_in_group

        if True or ( si.slot_type == Slot_type.HANDOVER ) or ( si.slot_type == Slot_type.CDS ) or ( si.slot_type == Slot_type.S0 ) or ( si.slot_type == Slot_type.S1 ) :
            si.dp_num = 'None'
                       
        for i in range(0, width+1):
            ci = ri.get_col(column+i)
            ci.append_slot(si)
    
    def write_bit_slot(self, row, column, width, source, text, color, data_port_number ):
#        print( 'write_bit_slot called for row={:d}, column={:d}, width={:d}'.format( row, column, width) )
        if (self.args.simple_mode):
            if Debug_Drawing : print( 'Row={:04d}, Col={:02d}, PortNum={:02d}, D="{:s}"'.format( row, column, data_port_number, text ) )
            for ii in range( 1, width ) :
                if Debug_Drawing : print( 'Row={:04d}, Col={:02d}, PortNum={:02d}, WWW'.format (row, column + 1, data_port_number, text ) )

        if not isinstance(source, bool):
            raise TypeError('Expected bool for source')
        if not isinstance(row, int):
            raise TypeError('Expected int for row')
        if not isinstance(column, int):
            raise TypeError('Expected int for column')
        if not isinstance(width, int):
            raise TypeError('Expected int for width')
        if not isinstance(text, str):
            raise TypeError('Expected str for text')
        if not isinstance(color, str):
            raise TypeError('Expected str for color')

        # Add channel & bit numbers to each bit slot, source 1 = write
        self.render_canvas.create_rectangle((column + 1.5) * self.COLUMN_SIZE,
                                            (row + 2 + 0.5 * (not source)) * self.ROW_SIZE + 2 * (
                                                source) + 0 * (not source),
                                            (column + 2.5 + width) * self.COLUMN_SIZE,
                                            (row + 2.5 + 0.5 * (not source)) * self.ROW_SIZE + 0 * (
                                                source) - 1 * (not source),
                                            fill=color, width=1)
        self.render_canvas.create_text((column + 2 + width/2) * self.COLUMN_SIZE,
                                       (row + 2.25 + 0.5 * (not source)) * self.ROW_SIZE + 0 * (
                                           source) - 1 * (not source),
                                       font=(self.APP_FONT, self.TEXT_SIZE - 2),
                                       justify=tk.CENTER,
                                       text=text)

    # Draws one data port in a canvas
    def draw_data_port(self, data_port, color):

        if Debug_Drawing : print( "draw_data_port called." )
        if not isinstance(data_port, DataPort):
            raise TypeError('Expected DataPort object, got: ' + str(type(data_port)))
        if not isinstance(color, str):
            raise TypeError('Expected str for color')
        if Debug_Drawing : print('draw_data_port' + str( data_port.number ) )
        # Check ranges of input parameters, should be ok based on earlier checking
        error_text = ''
        if data_port.device_number < DataPort.MIN_DEVICE_NUMBER or data_port.device_number > DataPort.MAX_DEVICE_NUMBER:
            error_text += 'Channel count out of range\n'
        if data_port.channels_REG < DataPort.MIN_CHANNELS or data_port.channels_REG > DataPort.MAX_CHANNELS:
            error_text += 'Channel count out of range\n'
        if data_port.channel_grouping_REG < DataPort.MIN_CHANNEL_GROUPING or data_port.channel_grouping_REG > DataPort.MAX_CHANNEL_GROUPING:
            error_text += 'Channel Grouping out of range\n'
        if data_port.channel_group_spacing_REG < DataPort.MIN_CHANNEL_GROUP_SPACING or data_port.channel_group_spacing_REG > \
                DataPort.MAX_CHANNEL_GROUP_SPACING:
            error_text += 'Change Group Spacing out of range\n'
        if data_port.sample_width_REG < DataPort.MIN_SAMPLE_WIDTH or data_port.sample_width_REG > DataPort.MAX_SAMPLE_WIDTH:
            error_text += 'Sample Width out of range\n'
        if data_port.sample_grouping_REG < DataPort.MIN_SAMPLE_GROUPING or data_port.sample_grouping_REG > DataPort.MAX_SAMPLE_GROUPING:
            error_text += 'Sample Grouping out of range\n'
        if data_port.interval_REG < DataPort.MIN_INTERVAL or data_port.interval_REG > DataPort.MAX_INTERVAL:
            error_text += 'Interval out of range\n'
        if data_port.skipping_numerator_REG < DataPort.MIN_SKIPPING_NUMERATOR or data_port.skipping_numerator_REG > DataPort.MAX_SKIPPING_NUMERATOR:
            error_text += 'Fractional Skipping out of range\n'
        if data_port.offset_REG < DataPort.MIN_OFFSET or data_port.offset_REG > DataPort.MAX_OFFSET:
            error_text += 'Offset out of range\n'
        if data_port.horizontal_start_REG < 0 or data_port.horizontal_start_REG >= self.interface.MAX_COLUMNS:
            error_text += 'Horizontal Start out of range\n'
        if data_port.horizontal_count_REG < 0 or data_port.horizontal_count_REG >= self.interface.MAX_COLUMNS:
            error_text += 'Horizontal Count out of range\n'
        if ( ( data_port.sample_width_REG + 1 ) * ( data_port.channels_REG + 1 ) * ( data_port.sample_grouping_REG  + 1) ) >  ( (data_port.horizontal_count_REG + 1 ) * ( data_port.interval_REG + 1 ) ) :
            error_text += 'A single sample frame does not fit in an interval"s worth of bit as width = ' + str( data_port.sample_width_REG ) + ', channels = ' + str( data_port.channels_REG ) + ' grouping = ' + str( data_port.sample_grouping_REG ) + ', count = ' + str( data_port.channels_REG ) + ', interval = ' + str( data_port.interval_REG )

        # Check some relationships
        if data_port.horizontal_start_REG + data_port.horizontal_count_REG >= self.interface.columns_per_row :
            error_text += 'horizontal_start(' + str( data_port.horizontal_start_REG ) + ') + horizontal_count(' + str( data_port.horizontal_count_REG ) + ') >= number of columns (exceeds last column)\n'
        if data_port.sri_REG :
            if data_port.channel_group_spacing_REG == 0 :
                error_text += 'Channel_Group_Spacing cannot be 0 when SRI is set\n'
            drive_in_group = ( data_port.sample_width_REG + 1 ) * ( data_port.sample_grouping_REG + 1 ) * ( data_port.channel_grouping_REG + 1 ) * ( data_port.bit_width_REG + 1 ) # for DataPort programming validation.   ### BUG, channel_grouping is not in this calculation properly.
            cadence_of_group = drive_in_group + data_port.channel_group_spacing_REG # for DataPort programming validation.
            if math.floor( ( data_port.horizontal_count_REG - data_port.horizontal_start_REG ) / cadence_of_group ) * cadence_of_group + drive_in_group > ( data_port.horizontal_count_REG + 1 ) :
                error_text += 'Group or Sample is incomplete when end of row encounters for data port\n'
        if data_port.offset_REG > data_port.interval_REG:
            error_text += 'Offset > Interval\n'
        if data_port.horizontal_start_REG >= self.interface.columns_per_row:
            error_text += 'Horizontal Start > Columns per Row\n'
        if data_port.horizontal_count_REG >= self.interface.columns_per_row:
            error_text += 'Horizontal Count > Columns per Row\n'
        if self.interface.cds_s0_handover_width > data_port.horizontal_start_REG and data_port.source_REG and data_port.handover:
            error_text += 'TA width > Horizontal Start\n'
        if data_port.tail_width_REG > self.interface.columns_per_row - data_port.horizontal_count_REG and data_port.source_REG:
            error_text += 'Tail width would overflow row\n'
        if data_port.bit_width_REG > self.interface.columns_per_row - data_port.horizontal_count_REG and data_port.source_REG:
            error_text += 'Bit width would overflow row\n'
        if data_port.bit_width_REG > data_port.horizontal_count_REG:
            error_text += 'Bit width would overflow horizontal_count\n'
        if (data_port.horizontal_count_REG + 1) % (data_port.bit_width_REG + 1) != 0:
            error_text += 'horizontal_count_REG + 1 should be a multiple of bit_width_REG + 1\n'
        if data_port.horizontal_count_REG >= self.interface.columns_per_row and data_port.source_REG and data_port.guard_REG:
            error_text += 'Post zero would overflow row\n'

        if len(error_text) > 0:
            return error_text

### Cut from here for Eddie
# This is run for each DataPort.
        # Raster our frame

        started = False
        Row = 0
        frac_accum = 0 # accumlate to decide when to skip this transport opportunity
        interval_counter = 0
        end_of_interval = False

        if data_port.channel_grouping_REG == 0 or data_port.channel_grouping_REG > ( data_port.channels_REG + 1 ):  # NDW check or data_port.sri_REG:
            effective_channel_group = data_port.channels_REG + 1
        else:
            effective_channel_group = data_port.channel_grouping_REG

        if Debug_Drawing : print ( 'about to raster' )
        data_port.reset()
        
        # Row counter is not part of the needed data port mechanism but is to support drawing
        for row_counter in range( 0, self.rows_in_frame, 1 ) : 
            if Debug_Drawing : print ( 'row_counter={},'.format( row_counter ) )
            Row += 1
            data_port.new_row( row_counter, self.interface.skipping_denominator_REG )
### To here for Eddie
            if True : # insert old method stuff here from files names scr
### Cut from here for Eddie
                last_bit_was_driven = 0
                column_counter = 0
                while column_counter < self.interface.columns_per_row :
                    width, rrrr, data_port.sample_number_in_group = data_port.try_bit( row_counter, column_counter, self.interface.skipping_denominator_REG )
                    if Debug_Drawing : print ( 'result from try_bit = "{}"'.format( width, rrrr ) )
                    if NOT_OWNED == rrrr :
                        rrrr = data_port.get_guard_or_tails()
                        if NOT_OWNED == rrrr :
                            if Debug_Drawing : print( 'not drawing this bit.' ) # note drawing
                            if last_bit_was_driven :
                                if data_port.source_REG and data_port.handover :
                                    self.draw_handover( row_counter, column_counter, data_port.device_number)
                                    self.update_col_in_frame_model(row_counter, column_counter, data_port.number,
                                                                   data_port.source_REG, 0, 'HANDOVER', data_port.sample_number_in_group)
                            last_bit_was_driven = False
                        elif "tail" == rrrr :
                            if Debug_Drawing : print( 'calling draw tail' )
                            self.draw_tail( row_counter, column_counter, data_port.device_number, color )
                            self.update_col_in_frame_model(row_counter, column_counter, data_port.number, data_port.source_REG, width, rrrr, 0 )
                            last_bit_was_driven = True
                        elif 'G' == rrrr :
                            self.write_bit_slot( row_counter, column_counter, width, data_port.source_REG, rrrr, color, data_port.number ) #, data_port.sample_grouping_REG - data_port.samples_remaining_in_sample_group, data_port.current_channel, data_port.current_bit_in_sample )
                            self.update_col_in_frame_model(row_counter, column_counter, data_port.number, data_port.source_REG,
                                                           width, rrrr, 0)
                            last_bit_was_driven = True
                        else :
                            error()
                    else :
                        self.write_bit_slot( row_counter, column_counter, width, data_port.source_REG, rrrr, color, data_port.number)
                        self.update_col_in_frame_model(row_counter, column_counter, data_port.number, data_port.source_REG,
                                                       width, rrrr, data_port.sample_number_in_group)
                        last_bit_was_driven = True
                        data_port.set_guards_and_tails() # A bit is owned so 
                        if data_port.source_REG:
                            self.check_bus_clash(row_counter, column_counter, data_port.device_number, 'write')
                        else:
                            self.check_bus_clash(row_counter, column_counter, data_port.device_number, 'read')
                    column_counter += width + 1
### To here for Eddie
                    
                    # more stuff here
                    # next gorup
                        

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
    MIN_SKIPPING_DENOMINATOR = 1
    MAX_SKIPPING_DENOMINATOR = 4096
    MIN_S0_WIDTH = 1
    MAX_S0_WIDTH = 8
    MIN_S1_WIDTH = 1
    MAX_S1_WIDTH = 8
    #MIN_S1_TAILS = 0
    #MAX_S1_TAILS = 3
    MIN_CDS_S0_HANDOVER_WIDTH = 0
    MAX_CDS_S0_HANDOVER_WIDTH = 8
    MIN_TAIL_WIDTH = 0
    MAX_TAIL_WIDTH = 2
    MIN_BIT_WIDTH = 0
    MAX_BIT_WIDTH = 2

    NUM_DATA_PORTS = 12

    def __init__(self):
        self.columns_per_row = 24
        self.s0s1_enabled = True
        self.s0_width = Interface.MIN_S0_WIDTH
        self.s1_width = Interface.MIN_S1_WIDTH
        #self.s1_tails = Interface.MIN_S1_TAILS
        self.s0_handover_enabled = True
        self.cds_guard_enabled = False
        self.cds_s0_handover_width = 1
        self.tail_width = Interface.MIN_TAIL_WIDTH
        self.skipping_denominator_REG = 16
        self.row_rate = 3072
        self._interval_lcm = 0 # This is likely broken
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
    def s1_width(self):
        return self._s1_width

    @s0_width.setter
    def s1_width(self, v):
        if type(v) != int:
            raise TypeError('S1 width must be int, got ' + str(type(v)))
        if v > Interface.MAX_S1_WIDTH:
            raise ValueError('S1 width must be <= ' + str(Interface.MAX_S0_WIDTH))
        if v < Interface.MIN_S1_WIDTH:
            raise ValueError('S1 width must be >= ' + str(Interface.MIN_S0_WIDTH))
        self._s1_width = v

    #@property
    #def s1_tails(self):
    #    return self._s1_tails

    #@s1_tails.setter
    #def s1_tails(self, v):
    #    if type(v) != int:
    #        raise TypeError('S1 tails must be int, got ' + str(type(v)))
    #    if v > Interface.MAX_S1_TAILS:
    #        raise ValueError('S1 tails must be <= ' + str(Interface.MAX_S1_TAILS))
    #    if v < Interface.MIN_S1_TAILS:
    #        raise ValueError('S1 tails must be >= ' + str(Interface.MIN_S1_TAILS))
    #    self._s1_tails = v
#
    @property
    def s0_handover_enabled(self):
        return self._s0_handover_enabled

    @s0_handover_enabled.setter
    def s0_handover_enabled(self, v):
        if type(v) != bool:
            raise TypeError('Draw S0 Handover must be bool, got ' + str(type(v)))
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
    def cds_s0_handover_width(self):
        return self._cds_s0_handover_width

    @cds_s0_handover_width.setter
    def cds_s0_handover_width(self, v):
        if type(v) != int:
            raise TypeError('CDS/S0 Handover width must be int, got ' + str(type(v)))
        if v > Interface.MAX_CDS_S0_HANDOVER_WIDTH:
            raise ValueError('CDS/S0 Handover width must be <= ' + str(Interface.MAX_CDS_S0_HANDOVER_WIDTH))
        if v < Interface.MIN_CDS_S0_HANDOVER_WIDTH:
            raise ValueError('CDS/S0 Handover width must be >= ' + str(Interface.MIN_CDS_S0_HANDOVER_WIDTH))
        self._cds_s0_handover_width = v

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
            raise ValueError('Tail width must be >= ' + str(Interface.MIN_TAIL_WIDTH))
        self._tail_width = v

    @property
    def skipping_denominator_REG(self):
        return self._skipping_denominator_REG

    @skipping_denominator_REG.setter
    def skipping_denominator_REG(self, v):
        if type(v) != int:
            raise TypeError('Skipping denominator must be int, got ' + str(type(v)))
        if v > Interface.MAX_SKIPPING_DENOMINATOR:
            raise ValueError('Skipping denominator must be <= ' + str(Interface.MAX_SKIPPING_DENOMINATOR))
        if v < Interface.MIN_SKIPPING_DENOMINATOR:
            raise ValueError('Skipping denominator must be >= ' + str(Interface.MIN_SKIPPING_DENOMINATOR))
        self._skipping_denominator_REG = v

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
    def interval_lcm(self):  # All LCM stuff is probably broken
        # This will be calculated rather than be an instance variable
        interval_list = []
        for count, data_port in enumerate(self.data_ports):
            if data_port.enabled:
                interval_list.append(
                    self.data_ports[ count ].interval_REG * self.skipping_denominator_REG + self.data_ports[ count ].skipping_numerator_REG)
            interval_list.append( self.skipping_denominator_REG )
        return self._interval_lcm

### Cut from here for Eddie
class DataPort:
### To here for Eddie
    count = 0

    # Data port ranges
    MIN_DEVICE_NUMBER = 0
    MAX_DEVICE_NUMBER = 12
    MIN_OFFSET = 0
    MIN_INTERVAL = 0
    MAX_INTERVAL = 4095
    MAX_OFFSET = MAX_INTERVAL
    MIN_CHANNELS = 0
    MAX_CHANNELS = 15
    MIN_SAMPLE_WIDTH = 0
    MAX_SAMPLE_WIDTH = 31
    MIN_SAMPLE_GROUPING = 0
    MAX_SAMPLE_GROUPING = 7
    MIN_CHANNEL_GROUPING = 0
    MAX_CHANNEL_GROUPING = MAX_CHANNELS
    MIN_CHANNEL_GROUP_SPACING = 0
    MAX_CHANNEL_GROUP_SPACING = MAX_CHANNELS
    MIN_SKIPPING_NUMERATOR = 0
    MAX_SKIPPING_NUMERATOR = Interface.MAX_SKIPPING_DENOMINATOR - 1
    MIN_SKIPPING_DENOMINATOR = 0
    MAX_SKIPPING_DENOMINATOR = Interface.MAX_SKIPPING_DENOMINATOR - 1
    MIN_H_START = 0
    MAX_H_START = Interface.MAX_COLUMNS - 1
    MIN_H_COUNT = 0
    MAX_H_COUNT = Interface.MAX_COLUMNS - 1
    MIN_TAIL_WIDTH = 0
    MAX_TAIL_WIDTH = Interface.MAX_TAIL_WIDTH
    MIN_BIT_WIDTH = 0
    MAX_BIT_WIDTH = Interface.MAX_BIT_WIDTH

    NUM_DP_PARAMETERS = 13 # This name is not accurate.  This is the count of the things that are not check boxes and also includes device number


    def __init__(self):
        self.name = 'DP' + str(DataPort.count)
        self.number = DataPort.count
        self.device_number = 0
        # All variables that contain "_REG" correspond to registers in the specification.
        # Example when the sample width is equal to 1 bit, sample_width_REG == 0.
        self.channels_REG = 0
        self.channel_grouping_REG = 0
        self.channel_group_spacing_REG = 0
        self.sample_width_REG =0
        self.sample_grouping_REG = 0
        self.interval_REG = 0
        self.skipping_numerator_REG = 0
        self.offset_REG = 0
        self.horizontal_start_REG = 4
        self.horizontal_count_REG = 0
        self.tail_width_REG = 0
        self.bit_width_REG = 0
        self.source_REG = True
        self.handover = False
        self.tail_REG = False
        self.guard_REG = False
        self.sri_REG = False
        
 ### Cut from here for Eddie
# The following variables are used for modeling with this tool and do not relate to actual implementation.
        self.enabled = False
        self.inManager = False
        self.sample_rate = 0
        
        # The following variables might correpond closely to a real implementation
        self.current_offset_in_interval = -1
        self.current_channel = 0
        self.current_sample_index = 0
        self.samples_remaining_in_sample_group = 0
        self.accumulated_fraction = 0
        self.tails_left = 0
        self.guards_left = 0
        self.channel_group_is_spacing = 0
        self.sample_number_in_group = 'u'
        
        # effect_channel_grouping is a helper variable that is calculated from channel_grouping_REG and channels_REG
        self.effective_channel_group = 0
        
        # These two variables are used for channel grouping.  Simpler implementations might exist.
        self.channel_group_end = 0
        self.channel_group_base = 0
        
        # The following variables are use for housekeeping
        # and error checking in this tools and would likely not exist in real implementations
        self.done_with_interval = False
        self.last_column_evaluated = -1
        self.last_row_evaluated = -1
        self.end_of_row = False
        self.started = False
        DataPort.count += 1
### To here for Eddie


    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, v):
        if type(v) != str:
            raise TypeError('Name must be a string, got ' + str(type(v)))
        self._name = v
    @property
    def device_number(self):
        return self._device_number

    @device_number.setter
    def device_number(self, v):
        if type(v) != int:
            raise TypeError('Device number must be int, got ' + str(type(v)))
        if v > DataPort.MAX_DEVICE_NUMBER:
            raise ValueError('Device number must be <= ' + str(DataPort.MAX_DEVICE_NUMBER))
        if v < DataPort.MIN_DEVICE_NUMBER:
            raise ValueError('Device number must be >= ' + str(DataPort.MIN_DEVICE_NUMBER))
        self._device_number = v
    @property
    def channels_REG(self):
        return self._channels_REG

    @channels_REG.setter
    def channels_REG(self, v):
        if type(v) != int:
            raise TypeError('Channels must be int, got ' + str(type(v)))
        if v > DataPort.MAX_CHANNELS:
            raise ValueError('Channels must be <= ' + str(DataPort.MAX_CHANNELS))
        if v < DataPort.MIN_CHANNELS:
            raise ValueError('Channels must be >= ' + str(DataPort.MIN_CHANNELS))
        self._channels_REG = v

    @property
    def channel_grouping_REG(self):
        return self._channel_grouping_REG

    @channel_grouping_REG.setter
    def channel_grouping_REG(self, v):
        if type(v) != int:
            raise TypeError('Channel grouping must be int, got ' + str(type(v)))
        if v > DataPort.MAX_CHANNEL_GROUPING:
            raise ValueError('Channel grouping must be <= ' + str(DataPort.MAX_CHANNEL_GROUPING))
        if v < DataPort.MIN_CHANNEL_GROUPING:
            raise ValueError('Channel grouping be >= ' + str(DataPort.MIN_CHANNEL_GROUPING))
        self._channel_grouping_REG = v

    @property
    def channel_group_spacing_REG(self):
        return self._channel_group_spacing_REG

    @channel_group_spacing_REG.setter
    def channel_group_spacing_REG(self, v):
        if type(v) != int:
            raise TypeError('Channel group spacing must be int, got ' + str(type(v)))
        if v > DataPort.MAX_CHANNEL_GROUP_SPACING:
            raise ValueError('Channel group spacing must be <= ' + str(DataPort.MAX_CHANNEL_GROUP_SPACING))
        if v < DataPort.MIN_CHANNEL_GROUP_SPACING:
            raise ValueError('Channel group spacing must be >= ' + str(DataPort.MIN_CHANNEL_GROUP_SPACING))
        self._channel_group_spacing_REG = v

    @property
    def sample_width_REG(self):
        return self._sample_width_REG

    @sample_width_REG.setter
    def sample_width_REG(self, v):
        if type(v) != int:
            raise TypeError('Sample width must be int, got ' + str(type(v)))
        if v > DataPort.MAX_SAMPLE_WIDTH:
            raise ValueError('Sample width must be <= ' + str(DataPort.MAX_SAMPLE_WIDTH))
        if v < DataPort.MIN_SAMPLE_WIDTH:
            raise ValueError('Sample width must be >= ' + str(DataPort.MIN_SAMPLE_WIDTH))
        self._sample_width_REG = v

    @property
    def sample_grouping_REG(self):
        return self._sample_grouping_REG

    @sample_grouping_REG.setter
    def sample_grouping_REG(self, v):
        if type(v) != int:
            raise TypeError('Sample grouping must be int, got ' + str(type(v)))
        if v > DataPort.MAX_SAMPLE_GROUPING:
            raise ValueError('Sample grouping must be <= ' + str(DataPort.MAX_SAMPLE_GROUPING))
        if v < DataPort.MIN_SAMPLE_GROUPING:
            raise ValueError('Sample grouping must be >= ' + str(DataPort.MIN_SAMPLE_GROUPING))
        self._sample_grouping_REG = v

    @property
    def interval_REG(self):
        return self._interval_REG

    @interval_REG.setter
    def interval_REG(self, v):
        if type(v) != int:
            raise TypeError('Interval must be int, got ' + str(type(v)))
        if v > DataPort.MAX_INTERVAL:
            raise ValueError('Interval must be <= ' + str(DataPort.MAX_INTERVAL))
        if v < DataPort.MIN_INTERVAL:
            raise ValueError('Interval must be >= ' + str(DataPort.MIN_INTERVAL))
        self._interval_REG = v

    @property
    def skipping_numerator_REG(self):
        return self._skipping_numerator_REG

    @skipping_numerator_REG.setter
    def skipping_numerator_REG(self, v):
        if type(v) != int:
            raise TypeError('Interval must be int, got ' + str(type(v)))
        if v > DataPort.MAX_SKIPPING_NUMERATOR:
            raise ValueError('Interval must be <= ' + str(DataPort.MAX_SKIPPING_NUMERATOR))
        if v < DataPort.MIN_SKIPPING_NUMERATOR:
            raise ValueError('Interval must be >= ' + str(DataPort.MIN_SKIPPING_NUMERATOR))
        self._skipping_numerator_REG = v

    @property
    def offset_REG(self):
        return self._offset_REG

    @offset_REG.setter
    def offset_REG(self, v):
        if type(v) != int:
            raise TypeError('Offset must be int, got ' + str(type(v)))
        if v > DataPort.MAX_OFFSET:
            raise ValueError('Offset must be <= ' + str(DataPort.MAX_OFFSET))
        if v < DataPort.MIN_OFFSET:
            raise ValueError('Offset must be >= ' + str(DataPort.MIN_OFFSET))
        self._offset_REG = v

    @property
    def horizontal_start_REG(self):
        return self._horizontal_start_REG

    @horizontal_start_REG.setter
    def horizontal_start_REG(self, v):
        if type(v) != int:
            raise TypeError('Horizontal start must be int, got ' + str(type(v)))
        if v > DataPort.MAX_H_START:
            raise ValueError('Horizontal start must be <= ' + str(DataPort.MAX_H_START))
        if v < DataPort.MIN_H_START:
            raise ValueError('Horizontal start must be >= ' + str(DataPort.MIN_H_START))
        self._horizontal_start_REG = v

    @property
    def horizontal_count_REG(self):
        return self._horizontal_count_REG

    @horizontal_count_REG.setter
    def horizontal_count_REG(self, v):
        if type(v) != int:
            raise TypeError('Horizontal stop must be int, got ' + str(type(v)))
        if v > DataPort.MAX_H_COUNT:
            raise ValueError('Horizontal Count must be <= ' + str(DataPort.MAX_H_COUNT))
        if v < DataPort.MIN_H_COUNT:
            raise ValueError('Horizontal Count must be >= ' + str(DataPort.MIN_H_COUNT))
        self._horizontal_count_REG = v

    @property
    def tail_width_REG(self):
        return self._tail_width_REG

    @tail_width_REG.setter
    def tail_width_REG(self, v):
        if type(v) != int:
            raise TypeError('Tail Width must be int, got ' + str(type(v)))
        if v > DataPort.MAX_TAIL_WIDTH:
            raise ValueError('Tail Width must be <= ' + str(DataPort.MAX_TAIL_WIDTH))
        if v < DataPort.MIN_TAIL_WIDTH:
            raise ValueError('Tail Width must be >= ' + str(DataPort.MIN_TAIL_WIDTH))
        self._tail_width_REG = v

    @property
    def bit_width_REG(self):
        return self._bit_width_REG

    @bit_width_REG.setter
    def bit_width_REG(self, v):
        if type(v) != int:
            raise TypeError('WideBit Width must be int, got ' + str(type(v)))
        if v > DataPort.MAX_BIT_WIDTH:
            raise ValueError('WideBit Width must be <= ' + str(DataPort.MAX_BIT_WIDTH))
        if v < DataPort.MIN_BIT_WIDTH:
            raise ValueError('WideBit Width must be >= ' + str(DataPort.MIN_BIT_WIDTH))
        self._bit_width_REG = v

    @property
    def source_REG(self):
        return self._source_REG

    @source_REG.setter
    def source_REG(self, v):
        if type(v) != bool:
            raise TypeError('Source enabled must be bool, got ' + str(type(v)))
        self._source_REG = bool(v)

    @property
    def handover(self):
        return self._handover

    @handover.setter
    def handover(self, v):
        if type(v) != bool:
            raise TypeError('Draw Handover must be bool, got ' + str(type(v)))
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
    def guard_REG(self):
        return self._guard_REG

    @guard_REG.setter
    def guard_REG(self, v):
        if type(v) != bool:
            raise TypeError('Guard enabled must be bool, got ' + str(type(v)))
        self._guard_REG = bool(v)

    @property
    def sri_REG(self):
        return self._sri_REG

    @sri_REG.setter
    def sri_REG(self, v):
        if type(v) != bool:
            raise TypeError('Multiple transport interval per row must be a bool, got ' + str( type( v ) ) )
        self._sri_REG = bool( v )

    @property
    def enabled(self):
        return self._enabled

    @enabled.setter
    def enabled(self, v):
        if type(v) != bool:
            raise TypeError('Data port enabled must be bool, got ' + str(type(v)))
        self._enabled = bool(v)

    @property
    def inManager(self):
        return self._inManager

    @inManager.setter
    def inManager(self, v):
        if type(v) != bool:
            raise TypeError('Data port inManager must be bool, got ' + str(type(v)))
        self._inManager = bool(v)

    @property
    def sample_number_in_group(self):
        return self._sample_number_in_group

    @sample_number_in_group.setter
    def sample_number_in_group( self, v):
        self._sample_number_in_group = v
    

    def set_guards_and_tails( self ) : # TODO: This method should move to the device (not dataport). The RHS would come from the active dataport
        self.tails_left = self.tail_width_REG
        self.guards_left = self.guard_REG
        #self.last_dataport_was_source = self.source_REG

    def get_guard_or_tails( self ): # TODO: This method should move to the device (not dataport)
        if self.source_REG :
            if ( self.guards_left ) :
                self.guards_left = False
                return 'G'
            if ( self.tails_left > 0 ) :
                self.tails_left -= 1
                return 'tail'
        return NOT_OWNED


### Cut from here for Eddie
    # The initial values might be quite different in a real implementation.
    # These values are chosen mostly due to artifacts of this tool.
    # This sequence is done both when the port is enabled or when the port is prepared.
    def reset( self ):
        self.current_offset_in_interval = -1
        self.last_row_evaluated = -1
        self.started = False
        self.done_with_interval = False
        self.end_of_row = False
        # these two should move to the device
        self.tails_left = 0
        self.guards_left = False
        self.accumulated_fraction = 0
        if Debug_Drawing : print( "dataport reset called" )

    def startInterval( self ) :
        if ( False != self.done_with_interval ) :
            messagebox.showwarning("Error!", "Data past eng of interval, check all sizes and interval and offset.")
        self.samples_remaining_in_sample_group = self.sample_grouping_REG
        self.channel_group_base = 0 # Like current_channel, starts at 0
        self.current_channel = 0
        if Debug_Drawing : print ("channel_grouping_Reg = ", self.channel_grouping_REG, " channels_REG = ", self.channels_REG )
        if self.channel_grouping_REG == 0 or self.channel_grouping_REG > self.channels_REG : # or self.sri_REG:
            self.effective_channel_grouping = self.channels_REG + 1
        else:
            self.effective_channel_grouping = self.channel_grouping_REG
            # effective_channel_grouping is an intermediate variable for clarity
        if Debug_Drawing : print ( "effective_channel_grouping = ", self.effective_channel_grouping )
        self.channel_group_end = self.effective_channel_grouping
        # as soon as (just after last bit in sample) the current channel gets here, it is time for spacing.

        self.current_bit_in_sample = self.sample_width_REG
        # TODO: this is where a call to fetch SampleGroup * Channels of audio from the file to playing out.


### To here for Eddie
### Cut from here for Eddie        
    # This is called for each possible column that could start a bit (or wide bit).  This is not called more than once per data bit.
    # try_bit returns a tuple containing
    # 1. the width (integer) of the "bit" (how many UIs) and
    # 2. the value to drive (a string coding ownership or the bit address withing the sample frames)
    def try_bit( self, row_number, column_number, denominator_REG ) :
        if Debug_Drawing : print( 'try_bit called with DP={:d}, row={:d}, column={:d}'.format( self.number, row_number, column_number ) )
        ret_value = 'error'

           
        if ( self.end_of_row or self.done_with_interval ) :
            if Debug_Drawing : print( 'leaving try_bit early due to end_of_row = ', self.end_of_row, ' or done_with_interval =', self.done_with_interval )
            return 0, NOT_OWNED, 0 # self.drive_guards_and_tails()

        if self.last_column_evaluated == column_number :
            raise ValueError ( "This column was already evaluated " )
        self.last_column_evaluated = column_number

        if self.started :
            # Rendering of bits starts when the column gets to horizontal_start
            if column_number == self.horizontal_start_REG :
                if Debug_Drawing : print( '    starting row, set done_with_row to false' )
                self.done_with_row = False
            if column_number < self.horizontal_start_REG :
                if Debug_Drawing : print( ' leaving try_bit early:   left of horizontal_start, not owning' )
                return 0, NOT_OWNED, 0
            if self.done_with_row or self.done_with_interval :
                if Debug_Drawing : print( ' leaving try_bit early:   driving any guard or tail as done_with_Row or done_with_interval or channel_group_is_spacing' )
                return 0, NOT_OWNED, 0 # self.drive_guards_and_tails()

            # Are we past the end of the row?
            elif column_number > self.horizontal_start_REG + self.horizontal_count_REG :
                if Debug_Drawing : print( '    hit h_stop, setting done_with_row True horizontal_start={}, hstop={}'.format(self.horizontal_start_REG, self.horizontal_count_REG ) )
                self.done_with_row = True
                self.channel_group_is_spacing = 0;
                if self.sri_REG :
                    if Debug_Drawing : print( "ending interval due to SRI" )
                    self.started = False
                    self.done_with_interval = True
                    self.end_of_row = True 
                return 0, NOT_OWNED, 0 # self.drive_guards_and_tails()
            
            else : # In between HSTART and ( HSTART + HCOUNT ), inclusive.
                self.sample_number_in_group = self.sample_grouping_REG - self.samples_remaining_in_sample_group
                if self.channel_group_is_spacing > 0 :
                    self.channel_group_is_spacing -= 1
                    if Debug_Drawing : print( " leaving try_bit early skipping bits due to spacing" )
                    return 0, NOT_OWNED, 0 # self.drive_guards_and_tails()

                # last_value_sent would be this value of this return  <--- what the heck does this mean?
                else : # done with any bit widening
                    if Debug_Drawing : print( '    Done with wide bit, going on to next bit (if it exists)' )
                    if ( self.current_bit_in_sample >= 0 ) :
                        if Debug_Drawing : print( '    There is at least one bit left in the current sample' )
                        ret_value = 'c' + str( self.current_channel ) + 'b' + str( self.current_bit_in_sample )
                        self.current_bit_in_sample -= 1
                    # ??? last_value_sent would be this value of this return
                    if self.current_bit_in_sample < 0 :
                        # Done will all bits in the current sample.  Go to the next channel or frame
                        if Debug_Drawing : print( '    going on to next channel (if it exists) channel_group_end = ', self.channel_group_end, " current_channel = ", self.current_channel )
                        self.current_channel += 1
                        if self.channel_group_end <= self.current_channel : # are we at the end of the channel group
                            if Debug_Drawing : print( '    end of channel group' )
                            self.samples_remaining_in_sample_group -= 1
                            if 0 > self.samples_remaining_in_sample_group : # done with sample group ?
                                if Debug_Drawing : print( 'finished sample group' )
                                self.sample_number_in_group += 1
                                if self.sample_number_in_group >= self.sample_grouping_REG :
                                    self.sample_number_in_group = 0
                                   # NDW Niel work here self.big_sample_group += self.sample_groupng_REG
                                if self.channel_group_end >= self.channels_REG + 1 : # Done with all channel groups
                                    if not self.sri_REG :
                                        if Debug_Drawing : print( 'done with interval' )
                                        self.done_with_interval = True
                                        self.end_of_row = True # NDW change for SRI
                                        self.started = False
                                    else :
                                        self.channel_group_is_spacing = self.channel_group_spacing_REG
                                        self.startInterval()
 
                                else : # we are not at the last channel and so need to go to the next channel group.
                                    if Debug_Drawing : print( "starting next channel group (After spacing)" )
                                    self.channel_group_base += self.effective_channel_grouping
                                    self.channel_group_end += self.effective_channel_grouping
                                    # clip when last group of channels is smaller
                                    if self.channel_group_end > self.channels_REG :
                                        self.channel_group_end = self.channels_REG + 1
                                    # reset sample group counter
                                    self.samples_remaining_in_sample_group = self.sample_grouping_REG
                                    self.current_bit_in_sample = self.sample_width_REG
                                    self.channel_group_is_spacing = self.channel_group_spacing_REG
                                    assert( self.current_channel == self.channel_group_base )
                                if 0 == self.channel_group_spacing_REG : # 0 means next row.
                                    self.end_of_row = True
                                else : # 1 for spacing means next column
                                    self.channel_group_is_spacing -= 1
                            else : # continue with current group of channels starting at the first channel in the group
                                self.current_bit_in_sample = self.sample_width_REG
                                self.current_channel = self.channel_group_base
                        else : # not the end of the channel group just go to the next channel
                            if Debug_Drawing : print( "    still in the channel group" )
                            self.current_bit_in_sample = self.sample_width_REG
                            # TODO: This is where the output shift register would get loaded with a new sample
                if Debug_Drawing : print( "normal end of bit" )


        else:
            ret_value = NOT_OWNED

        if ( self.sri_REG ) :
            if Debug_Drawing : print( "SRI noted near end") 
            if ( column_number == self.horizontal_start_REG + self.horizontal_count_REG ) :
                # for SRI, these variable need to be reset so that things start the same each row.
                if Debug_Drawing : print( "special SRI check worked" )
                self.started = False
                self.done_with_interval = True
                self.end_of_row = True # NDW change for SRI???
        self.sample_number_in_group = self.sample_grouping_REG - self.samples_remaining_in_sample_group
        return self.bit_width_REG, ret_value, self.sample_number_in_group


    def new_row( self, row_number, denominator_REG ) :
        if Debug_Drawing : print( 'new_row called with row={:d}, denominator_REG={:d}'.format( row_number, denominator_REG ) )
        ret_value = 'error'
        if self.sri_REG :
            self.startInterval()
            self.end_of_row = False
            self.started = True
            self.done_with_interval = False
            self.channel_group_is_spacing = 0
            ret_value = 'started'
        else :
            assert( self.last_row_evaluated + 1 == row_number )
            self.last_row_evaluated = row_number
            self.last_column_evaluatted = -1
            self.end_of_row = False
            self.current_offset_in_interval += 1
            # Current_offset_in_interval should be 0 anytime an SSP occurs
            # and is reset when channels are enabled or prepared.

            assert( self.current_offset_in_interval <= self.interval_REG + 1 )
            if self.current_offset_in_interval == ( self.interval_REG + 1 ) :
                if Debug_Drawing : print( 'end of interval reached for DP={:d}, row={:d}'.format( self.number, row_number ) )
                self.current_offset_in_interval = 0
                self.done_with_interval = False
            if self.current_offset_in_interval == self.offset_REG:
                if Debug_Drawing : print( 'offset matched for DP={:d}, row={:d}'.format( self.number, row_number ) )
                assert( not self.started )
                # These lines are for the optional skipping feature
                if ( self.skipping_numerator_REG != 0 ) :
                    self.accumulated_fraction += self.skipping_numerator_REG
                    if self.accumulated_fraction >= denominator_REG : # comment about skipping.
                        self.done_with_interval = True
                        self.accumulated_fraction -= denominator_REG
                        if Debug_Drawing : print( 'leaving new_row early due to skipping' )
                        ret_value = 'skipping'
                        return
                    
                if Debug_Drawing : print( '    will drive bits this interval' )
                
                self.started = True
                self.startInterval()
                ret_value = 'starting'
        return ret_value
        
### To here for Eddie

###############################
#                             #
#       COMMAND LINE          #
#     ARGUMENT PARSING        #
#                             #
###############################

def parse_cmdline():
    parser = argparse.ArgumentParser(description='')
    parser.add_argument('-b', '--batch', required=False,
                        dest='batch_mode', default=False, action='store_true',
                        help='Execute the application in batch mode')
    
    parser.add_argument('-s', '--simple', required=False,
                        dest='simple_mode', default=False, action='store_true',
                        help='Execute the application using simple output if in batch mode')
    
    parser.add_argument('-c', '--config_file', metavar='file', required=False,
                                          dest='config_filename', action='store',
                                          help='System/dataports config file (CSV)')

    parser.add_argument('-o', '--output_frame_file', metavar='file', required=False,
                                          dest='out_frame_filename', default='frame.json', action='store',
                                          help='Frame model output file (JSON)')    

    args      = parser.parse_args()
    if Debug_FileIO : print(args)
    return args

def openfile(filename, mode):
   try:
      f = open(filename, mode)
   except OSError:
      print("openfile: Cannot open file", filename )
      exit(1)
   else:
      return f

###############################

if __name__ == '__main__':
    args = parse_cmdline()
    root = tk.Tk()
    app = App(root, args)

    while True:
        try:
            app.mainloop()
            break
        except UnicodeDecodeError:
            # Catches a known TCL/TK issues
            pass
