"""Out-of-core results store for decoded commands (Apache Arrow / Parquet).

Milestone 1 keeps the command stream — sparse relative to the capture — in an
Arrow table, memory-mappable and filterable, savable to Parquet. (Audio goes to
a memmap + pyramid store in M2; the register-event log is derived from the
WriteA32/Commit commands here.)
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List

import pyarrow as pa
import pyarrow.parquet as pq

# NOTE: this store is a display/persistence cache — it is NOT fed back into register
# replay. The live register/grid state always replays the IN-MEMORY decoder output
# (Session._build_register_files -> swi3score.registers_from_commands), never a
# reloaded table. So fields only the replay needs (group_mask, has_sync_point,
# row_delay, bus_row) are intentionally omitted here; add them if the store ever
# drives replay.
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
    # memory_map=False: read the table fully into RAM rather than keeping an mmap
    # handle open on the file for the table's lifetime. The command table is small
    # (commands, not the audio pyramid), so the zero-copy win is negligible, while
    # a lingering handle LOCKS the file on Windows — blocking deletion/overwrite of
    # a temp or to-be-rewritten store (surfaced as a PermissionError tearing down a
    # TemporaryDirectory, and would also block a re-decode overwriting the store).
    return pq.read_table(path, memory_map=False)


def register_writes(commands: Iterable[Dict[str, Any]]):
    """Yield (start_sample, address, data_bytes, device_mask) for each CRC-valid
    WriteA32 — the raw material for the register-state timeline / register view."""
    for c in commands:
        if c.get("command") == "WriteA32" and c.get("crc_valid") and c.get("has_address"):
            yield (c["start_sample"], c["address"], c["data"], c["device_mask"])
