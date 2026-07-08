"""Export the decoded command stream to CSV (one row per phase).

Qt-free so it's usable headless and from tests. Address is resolved to its
register/field label via the RegisterMap, and each command is tagged with any
decode error.
"""
from __future__ import annotations

import csv
from typing import List, Optional

from ..analysis import command_error
from ..analysis.responses import response_summary

_HEADER = ["start_sample", "end_sample", "command", "phase", "devices",
           "address", "register", "data_hex", "crc", "response", "error"]


def _devices(mask: int) -> str:
    return ";".join(str(i) for i in range(12) if mask & (1 << i)) or "-"


def _register_label(rmap, cmd: dict) -> str:
    if not cmd.get("has_address") or rmap is None:
        return ""
    res = rmap.resolve(cmd["address"])
    if not res:
        return ""
    name = res.register.name
    if res.dp_index is not None:
        name = f"DP{res.dp_index}.{name}"
    if res.rank in ("NEXT", "CURR"):
        name += f"[{res.rank}]"
    return f"{res.block}.{name}"


def _response(cmd: dict) -> str:
    return response_summary(cmd)


def write_commands_csv(commands: List[dict], path: str, rmap=None) -> str:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(_HEADER)
        for c in commands:
            data = c.get("data") or b""
            crc = "" if not c.get("has_manager_packet") else ("OK" if c.get("crc_valid") else "BAD")
            w.writerow([
                c.get("start_sample", 0), c.get("end_sample", 0),
                c.get("command", ""), c.get("phase", ""),
                _devices(c.get("device_mask", 0)),
                f"0x{c['address']:08X}" if c.get("has_address") else "",
                _register_label(rmap, c),
                data.hex(" ") if data else "",
                crc, _response(c), command_error(c) or "",
            ])
    return path
