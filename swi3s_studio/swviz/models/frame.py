"""Frame model classes for representing SWI3S frames

The frame model represents the complete structure of a SoundWire I3S frame,
organized as rows and columns of slots.
"""

from __future__ import annotations

from typing import List, Union
import json
from enum import Enum

from .enums import DirectionType, SlotType


class FrameModel:
    """Complete frame model representing all rows and columns"""

    def __init__(self, n_rows: int = 0, n_cols: int = 2) -> None:
        self.row_info: List[RowInfo] = []
        for i in range(0, n_rows):
            self.row_info.append(RowInfo(n_cols, i))

    def get_row(self, i: int) -> RowInfo:
        return self.row_info[i]


class RowInfo:
    """Information about a single row in the frame"""

    def __init__(self, n_col: int, row_number: int) -> None:
        self.row_num: int = row_number
        self.col_info: List[ColInfo] = [ColInfo(i) for i in range(0, n_col)]

    def get_col(self, i: int) -> ColInfo:
        return self.col_info[i]


class ColInfo:
    """Information about a single column in a row"""

    def __init__(self, col_number: int) -> None:
        self.col_num: int = col_number
        self.slot_info: List[SlotInfo] = []

    def append_slot(self, slot: SlotInfo) -> None:
        self.slot_info.append(slot)


class SlotInfo:
    """Information about a single slot in the frame"""

    def __init__(self) -> None:
        self.slot_type: SlotType  = SlotType.DATA
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
