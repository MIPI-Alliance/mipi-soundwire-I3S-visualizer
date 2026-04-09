"""
Copyright (c) 2023 MIPI Alliance and other contributors. All Rights Reserved.

JSON file handler for exporting frame models and bus models.
"""

import json
import os
import logging
from typing import Any

# Import encoders from models module
from src.models.frame import SimpleJSONEncoder
from src.models.bus_model import BusModel, BusModelJSONEncoder

logger = logging.getLogger('swi3s_visualizer.io')


class JSONHandler:
    """Handles JSON file I/O for frame models and bus models."""

    @staticmethod
    def save_frame_model(filename: str, model: Any, batch_mode: bool = False) -> None:
        """Save frame model to JSON file (legacy format).

        Args:
            filename: Path to JSON file to create
            model: Frame model object to save
            batch_mode: Whether running in batch mode (creates directories if needed)

        Raises:
            OSError: If file cannot be written
            TypeError: If model cannot be serialized to JSON
        """
        try:
            if batch_mode:
                # In batch mode, create parent directories if they don't exist
                parent_dir = os.path.dirname(filename)
                if parent_dir:
                    os.makedirs(parent_dir, exist_ok=True)

            with open(filename, 'w') as fh:
                json.dump(model, fh, cls=SimpleJSONEncoder, indent=4)
        except (OSError, IOError) as e:
            logger.error(f'Failed to write JSON file {filename}: {e}')
            raise
        except TypeError as e:
            logger.error(f'Failed to serialize model to JSON: {e}')
            raise

    @staticmethod
    def save_bus_model(filename: str, model: BusModel, batch_mode: bool = False) -> None:
        """Save bus model to JSON file (new sequential format).

        Args:
            filename: Path to JSON file to create
            model: BusModel object to save
            batch_mode: Whether running in batch mode (creates directories if needed)

        Raises:
            OSError: If file cannot be written
            TypeError: If model cannot be serialized to JSON
        """
        try:
            if batch_mode:
                # In batch mode, create parent directories if they don't exist
                parent_dir = os.path.dirname(filename)
                if parent_dir:
                    os.makedirs(parent_dir, exist_ok=True)

            with open(filename, 'w') as fh:
                json.dump(model, fh, cls=BusModelJSONEncoder, indent=2)

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
            with open(filename, 'r') as fh:
                data = json.load(fh)

            # Validate required fields
            required = ['num_rows', 'num_columns', 'bits']
            for field in required:
                if field not in data:
                    raise ValueError(f"Missing required field: {field}")

            # Create BusModel
            from src.models.bus_model import BitInfo, ClashType
            from src.models.enums import SlotType, DirectionType

            bus_model = BusModel(
                num_rows=data['num_rows'],
                num_columns=data['num_columns'],
                row_rate=data.get('row_rate', 3072.0)  # Default if not present
            )

            # Load bits - new hierarchical format: bits is dict keyed by bit_index
            # Each entry has row, column, and slots array
            bits_data = data['bits']
            for bit_index_str, bit_entry in bits_data.items():
                bit_index = int(bit_index_str)
                for slot_data in bit_entry['slots']:
                    bit_info = BitInfo(
                        bit_index=bit_index,
                        slot=SlotType[slot_data['slot']],
                        direction=DirectionType[slot_data['direction']],
                        device=slot_data['device'],
                        dp=slot_data.get('dp'),
                        channel=slot_data.get('channel', 0),
                        sample=slot_data.get('sample', 0),
                        bit=slot_data.get('bit', 0),
                        clash=ClashType[slot_data.get('clash', 'NONE')],
                    )
                    bus_model.add_bit(bit_info)

            # Load clash lists
            bus_model.bus_clashes = data.get('bus_clashes', [])
            bus_model.device_clashes = data.get('device_clashes', [])
            bus_model.read_overlaps = data.get('read_overlaps', [])

            # Load warnings (new format)
            # Warnings are stored as {"truncation": [0, 1], "validation": [2]}
            # We reconstruct the internal warning lists from these
            warnings_data = data.get('warnings', {})
            if 'truncation' in warnings_data:
                for dp_index in warnings_data['truncation']:
                    # Reconstruct as tuple (dp_name, 0, 0) - actual values not stored
                    bus_model.interval_overflow_warnings.append((f"DP{dp_index}", 0, 0))
            if 'validation' in warnings_data:
                # For validation issues, we create placeholder ValidationResults
                from src.utils.validators import ValidationResult
                for dp_index in warnings_data['validation']:
                    result = ValidationResult()
                    bus_model.validation_issues.append((f"DP{dp_index}", result))

            logger.info(f'Bus model loaded from {filename}')
            return bus_model

        except (OSError, IOError) as e:
            logger.error(f'Failed to read JSON file {filename}: {e}')
            raise
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.error(f'Failed to parse bus model JSON: {e}')
            raise ValueError(f'Invalid bus model JSON: {e}')
