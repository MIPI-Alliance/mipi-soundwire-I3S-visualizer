"""Frame model classes for representing SWI3S frames

The frame model represents the complete structure of a SoundWire I3S frame,
organized as rows and columns of slots.
"""

from __future__ import annotations

from typing import List, Union
import json
from enum import Enum

from .enums import DirectionType, SlotType


class Frame_model:
    """Complete frame model representing all rows and columns"""

    def __init__(self, n_rows: int = 0, n_cols: int = 2) -> None:
        self.row_info: List[Row_info] = []
        for i in range(0, n_rows):
            self.row_info.append(Row_info(n_cols, i))

    def get_row(self, i: int) -> Row_info:
        return self.row_info[i]


class Row_info:
    """Information about a single row in the frame"""

    def __init__(self, n_col: int, row_number: int) -> None:
        self.row_num: int = row_number
        self.col_info: List[Col_info] = [Col_info(i) for i in range(0, n_col)]

    def get_col(self, i: int) -> Col_info:
        return self.col_info[i]


class Col_info:
    """Information about a single column in a row"""

    def __init__(self, col_number: int) -> None:
        self.col_num: int = col_number
        self.slot_info: List[Slot_info] = []

    def append_slot(self, slot: Slot_info) -> None:
        self.slot_info.append(slot)


class Slot_info:
    """Information about a single slot in the frame"""

    def __init__(self) -> None:
        self.slot_type: SlotType  = SlotType.NORMAL
        self.dir: DirectionType        = DirectionType.SINK
        self.device_num: int = 0
        self.dp_num: Union[int, str]     = 0
        self.sample: int = 0  # Absolute sample counter
        self.channel: int    = 0
        self.bit: int    = 0


class SimpleJSONEncoder(json.JSONEncoder):
    """JSON encoder that handles model classes with __dict__ and Enums"""

    def default(self, o):  # type: ignore[override]
        if hasattr(o, "__dict__"):
            d = {}
            for key, value in o.__dict__.items():
                if not key.startswith("_"):
                    if (isinstance(value, Enum)):
                        d[key] = value.name
                    else:
                        d[key] = value
            return d
        return super().default(o)
