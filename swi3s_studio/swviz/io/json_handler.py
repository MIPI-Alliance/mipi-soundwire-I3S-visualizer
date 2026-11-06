"""
Copyright (c) 2023 MIPI Alliance and other contributors. All Rights Reserved.

JSON file handler for bus models.
"""

import json
import logging
import math
import os
from typing import Dict, List, Optional

from ...fsutil import write_whole
from ..models.bus_model import BusModel, BusModelJSONEncoder

logger = logging.getLogger('swi3s_visualizer.io')


def _index_list(data: dict, key: str, limit: int) -> list:
    """The list of bit indices `data[key]` (absent: empty); every one is an int in [0, limit)."""
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f'{key} must be a list of bit indices')
    for index in value:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < limit:
            raise ValueError(f'{key} holds {index!r}, not a bit index below {limit}')
    return value


def _int_or_none(slot: dict, key: str, where: str, *, required: bool = False,
                 nullable: bool = True, default=None):
    """`slot[key]` as an int (or None when `nullable`); a missing key is `default`, or an
    error when `required`. Anything else, a bool included, names the field and its bit."""
    if key not in slot:
        if required:
            raise ValueError(f'{where}: {key} is missing')
        return default
    value = slot[key]
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f'{where}: {key} must be an integer'
                         f'{" or null" if nullable else ""}, got {value!r}')
    return value


def _enum_member(enum_cls, slot: dict, key: str, where: str, default=None):
    """The `enum_cls` member named by `slot[key]`; a missing key is `default` (a name)."""
    name = slot.get(key, default)
    if not isinstance(name, str) or name not in enum_cls.__members__:
        raise ValueError(f'{where}: {key} {name!r} is not a {enum_cls.__name__} name')
    return enum_cls[name]


def _is_finite(value) -> bool:
    """True when `value` is a finite number; an int beyond float range is not."""
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _port_list(warnings_data: dict, key: str) -> list:
    """The data-port numbers `warnings_data[key]` (absent: empty); each a non-negative int."""
    value = warnings_data.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f'warnings.{key} must be a list of data-port numbers')
    for dp_index in value:
        if isinstance(dp_index, bool) or not isinstance(dp_index, int) or dp_index < 0:
            raise ValueError(f'warnings.{key} holds {dp_index!r}, not a data-port number')
    return value


def _write_text_whole(path: str, text: str, newline: Optional[str] = None) -> None:
    """Write `text` (UTF-8) to `path` through `fsutil.write_whole`: a new file beside `path`
    moved onto it once complete, so a failed write leaves an existing `path` as it was and
    no partial file behind. `newline` is the text-mode argument of `open` ('' writes line
    endings as they are). The new file takes an existing target's mode. A folder that
    cannot hold a second file is written to `path` directly."""
    write_whole(path, lambda fh: fh.write(text), mode='w', encoding='utf-8', newline=newline)


class JSONHandler:
    """Handles JSON file I/O for bus models."""

    @staticmethod
    def save_bus_model(filename: str, model: BusModel, batch_mode: bool = False) -> None:
        """Save bus model to JSON file (sequential format). The file is written whole or
        not at all: a model that cannot be serialized or a write that fails leaves an
        existing file as it was.

        Args:
            filename: Path to JSON file to create
            model: BusModel object to save
            batch_mode: Whether running in batch mode (creates directories if needed)

        Raises:
            OSError: If file cannot be written
            TypeError: If model cannot be serialized to JSON
            ValueError: If the model has fewer than one row or one column (the loader
                refuses such a file)
        """
        if model.num_rows < 1 or model.num_columns < 1:
            raise ValueError(f'a bus model needs at least one row and one column, got '
                             f'{model.num_rows} x {model.num_columns}')
        try:
            if batch_mode:
                # In batch mode, create parent directories if they don't exist
                parent_dir = os.path.dirname(filename)
                if parent_dir:
                    os.makedirs(parent_dir, exist_ok=True)

            _write_text_whole(filename, json.dumps(model, cls=BusModelJSONEncoder, indent=2))

            logger.info(f'Bus model saved to {filename}')
        except (OSError, IOError) as e:
            logger.error(f'Failed to write JSON file {filename}: {e}')
            raise
        except TypeError as e:
            logger.error(f'Failed to serialize bus model to JSON: {e}')
            raise

    @staticmethod
    def load_bus_model(filename: str) -> BusModel:
        """Load bus model from JSON file.

        Args:
            filename: Path to JSON file to read

        Returns:
            BusModel object

        Raises:
            OSError: If file cannot be read
            ValueError: If JSON is invalid or missing required fields
        """
        try:
            with open(filename, 'r', encoding='utf-8') as fh:
                data = json.load(fh)

            # Validate required fields
            if not isinstance(data, dict):
                raise ValueError('Top-level JSON value must be an object')
            required = ['num_rows', 'num_columns', 'bits']
            for field in required:
                if field not in data:
                    raise ValueError(f"Missing required field: {field}")
            for field in ('num_rows', 'num_columns'):
                value = data[field]
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ValueError(f'{field} must be a positive integer, got {value!r}')
            if not isinstance(data['bits'], dict):
                raise ValueError('bits must be an object keyed by bit index')

            row_rate = data.get('row_rate', 3072.0)  # Default if not present
            if (isinstance(row_rate, bool) or not isinstance(row_rate, (int, float))
                    or not _is_finite(row_rate) or row_rate <= 0):
                raise ValueError(f'row_rate must be a finite positive number, got {row_rate!r}')
            total_bits = data['num_rows'] * data['num_columns']

            # Create BusModel
            from ..models.bus_model import BitInfo, ClashType
            from ..models.enums import DirectionType, SlotType

            bus_model = BusModel(
                num_rows=data['num_rows'],
                num_columns=data['num_columns'],
                row_rate=row_rate
            )

            # Load bits - bits is a dict keyed by bit_index
            # Each entry has row, column, and slots array
            bits_data = data['bits']
            for bit_index_str, bit_entry in bits_data.items():
                bit_index = int(bit_index_str)
                if not 0 <= bit_index < total_bits:
                    raise ValueError(f'bit index {bit_index} is outside the {total_bits}-bit frame')
                if not isinstance(bit_entry, dict) or not isinstance(bit_entry.get('slots'), list):
                    raise ValueError(f'bit {bit_index_str}: slots must be a list')
                for slot_data in bit_entry['slots']:
                    if not isinstance(slot_data, dict):
                        raise ValueError(f'bit {bit_index_str}: each slot must be an object')
                    where = f'bit {bit_index_str}'
                    bit_info = BitInfo(
                        bit_index=bit_index,
                        slot=_enum_member(SlotType, slot_data, 'slot', where),
                        direction=_enum_member(DirectionType, slot_data, 'direction', where),
                        device=_int_or_none(slot_data, 'device', where,
                                            required=True, nullable=False),
                        dp=_int_or_none(slot_data, 'dp', where),
                        channel=_int_or_none(slot_data, 'channel', where,
                                             nullable=False, default=0),
                        # Non-payload slots (guard/tail/CDS/…) have sample/bit = None;
                        # default to None so the round trip preserves that (0 is a
                        # valid payload sample and would corrupt mismatch checks).
                        sample=_int_or_none(slot_data, 'sample', where),
                        bit=_int_or_none(slot_data, 'bit', where),
                        clash=_enum_member(ClashType, slot_data, 'clash', where, 'NONE'),
                    )
                    # The clash lists are assigned below, so the bit need not track them.
                    bus_model.add_bit(bit_info, track_clashes=False)

            # Load clash lists. A list the file omits is derived from the slots' clash
            # flags (DIFFERENT_DEVICE -> bus_clashes, SAME_DEVICE -> device_clashes), each
            # index once, in the order the bits were read; read_overlaps has no slot flag.
            flagged: Dict[ClashType, List[int]] = {ClashType.DIFFERENT_DEVICE: [],
                                                   ClashType.SAME_DEVICE: []}
            for bit_info in bus_model.bits:
                seen = flagged.get(bit_info.clash)
                if seen is not None and bit_info.bit_index not in seen:
                    seen.append(bit_info.bit_index)
            bus_model.bus_clashes = (_index_list(data, 'bus_clashes', total_bits)
                                     if 'bus_clashes' in data
                                     else flagged[ClashType.DIFFERENT_DEVICE])
            bus_model.device_clashes = (_index_list(data, 'device_clashes', total_bits)
                                        if 'device_clashes' in data
                                        else flagged[ClashType.SAME_DEVICE])
            bus_model.read_overlaps = _index_list(data, 'read_overlaps', total_bits)

            # Load warnings
            # Warnings are stored as {"truncation": [0, 1], "validation": [2]}
            # We reconstruct the internal warning lists from these
            warnings_data = data.get('warnings', {})
            if not isinstance(warnings_data, dict):
                raise ValueError('warnings must be an object')
            for dp_index in _port_list(warnings_data, 'truncation'):
                # Reconstruct as tuple (dp_name, 0, 0) - actual values not stored
                bus_model.warnings.interval_overflow_warnings.append((f"DP{dp_index}", 0, 0))
            validation_ports = _port_list(warnings_data, 'validation')
            if validation_ports:
                # For validation issues, we create placeholder ValidationResults
                from ..utils.validators import ValidationResult
                for dp_index in validation_ports:
                    result = ValidationResult()
                    bus_model.warnings.validation_issues.append((f"DP{dp_index}", result))

            logger.info(f'Bus model loaded from {filename}')
            return bus_model

        except (OSError, IOError) as e:
            logger.error(f'Failed to read JSON file {filename}: {e}')
            raise
        except (json.JSONDecodeError, KeyError, ValueError, TypeError, AttributeError,
                OverflowError, RecursionError) as e:
            logger.error(f'Failed to parse bus model JSON: {e}')
            raise ValueError(f'Invalid bus model JSON: {e}') from e
