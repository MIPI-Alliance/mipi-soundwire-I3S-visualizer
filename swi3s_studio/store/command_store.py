"""Command table for decoded commands (Apache Arrow / Parquet), a persisted cache with no reader.

Keeps the command stream — sparse relative to the capture — in an Arrow table,
filterable and savable to Parquet; `load_commands` reads a saved table fully into memory.
Audio has its own memmap + pyramid store (`audio_store`). `register_writes` yields the
CRC-valid, single-device WriteA32 rows only (Commit rows are in the table, flagged is_commit,
and are not yielded).
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

import pyarrow as pa
import pyarrow.parquet as pq

# NOTE: this store is a persistence cache that nothing in the application reads back:
# the Commands table and every export read the IN-MEMORY decoder output
# (`Session.commands`), and register replay does too (Session._build_register_files ->
# swi3score.registers_from_commands). A command's fields not in `_FIELDS` are listed in
# `OMITTED_FIELDS`; add them to the schema if anything ever reads the table.
OMITTED_FIELDS = (
    "bus_row", "effective_row", "enablech_curr_error", "group_mask", "has_manager_packet",
    "has_sync_point", "header_valid", "packet_length", "read_only_write_error",
    "row_delay", "row_delay_error", "running_port_write_error", "unexpected_ssp_error",
    "unexpected_ssp_mid_interval",
)
_FIELDS = [
    ("command", pa.string()),
    ("phase", pa.string()),
    ("device_mask", pa.uint16()),
    ("opcode", pa.uint8()),
    ("has_address", pa.bool_()),
    ("address", pa.uint32()),
    ("data", pa.binary()),
    ("crc_valid", pa.bool_()),
    ("peripheral_response", pa.int16()),
    ("manager_response", pa.int16()),
    ("is_commit", pa.bool_()),
    ("commit_confirmed", pa.bool_()),
    ("has_read_data", pa.bool_()),
    ("read_data", pa.binary()),
    ("read_data_crc_valid", pa.bool_()),
    ("ping_status", pa.list_(pa.int16())),
    ("start_sample", pa.uint64()),
    ("end_sample", pa.uint64()),
]
SCHEMA = pa.schema(_FIELDS)


def commands_to_table(commands: List[Dict[str, Any]]) -> pa.Table:
    cols: Dict[str, list] = {name: [] for name, _ in _FIELDS}
    for c in commands:
        for name, _ in _FIELDS:
            if name == "ping_status":
                cols[name].append(c.get("ping_status"))  # list[int] or None
            else:
                cols[name].append(c.get(name))
    return pa.table({name: pa.array(cols[name], type=typ) for name, typ in _FIELDS},
                    schema=SCHEMA)


def save_commands(table: pa.Table, path: str) -> None:
    pq.write_table(table, path)


def load_commands(path: str) -> pa.Table:
    # memory_map=False: read the table fully into RAM. An open mmap handle locks the
    # file on Windows, blocking deletion or overwrite of the store.
    return pq.read_table(path, memory_map=False)


def register_writes(commands: Iterable[Dict[str, Any]]):
    """Yield (start_sample, address, data_bytes, device_mask) for each WriteA32 the register
    model applies: CRC-valid, addressed, and selecting exactly one device
    (`registers.selects_one_device`). The register view replays through the native core, not
    through this."""
    from ..model.registers import selects_one_device
    for c in commands:
        if (c.get("command") == "WriteA32" and c.get("crc_valid") and c.get("has_address")
                and selects_one_device(c.get("device_mask", 0))):
            yield (c["start_sample"], c["address"], c["data"], c["device_mask"])
