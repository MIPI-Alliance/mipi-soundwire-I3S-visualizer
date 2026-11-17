"""Export the decoded command stream to CSV (one row per phase).

Qt-free so it's usable headless and from tests. Address is resolved to its
register/field label via the RegisterMap, else the addressed device's peripheral
(vendor) register map, else the address region's name, as the Commands table resolves
it; each command is tagged with any decode error.
"""
from __future__ import annotations

import csv
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..analysis import command_error
from ..analysis.responses import command_payload, response_summary
from ..fsutil import write_whole

_HEADER = ["start_sample", "end_sample", "command", "phase", "devices",
           "address", "register", "data_hex", "crc", "response", "error"]


def _devices(mask: int) -> str:
    return ";".join(str(i) for i in range(12) if mask & (1 << i)) or "-"


def _device(mask: int) -> Optional[int]:
    """The command's single target device (the first set bit of its mask), or None."""
    return next((i for i in range(12) if mask & (1 << i)), None)


def _register_label(rmap, cmd: dict, peripheral_maps: Optional[Dict[int, Any]] = None) -> str:
    if not cmd.get("has_address") or rmap is None:
        return ""
    res = rmap.resolve(cmd["address"])
    if not res:
        dev = _device(int(cmd.get("device_mask", 0) or 0))
        pmap = (peripheral_maps or {}).get(dev) if dev is not None else None
        preg = pmap.resolve(cmd["address"]) if pmap is not None else None
        if preg is not None:
            return _text_cell(preg.name)
        from ..model.registers import region_name
        return region_name(cmd["address"])
    name = res.register.name
    if res.dp_index is not None:
        name = f"DP{res.dp_index}.{name}"
    if res.rank in ("NEXT", "CURR"):
        name += f"[{res.rank}]"
    return f"{res.block}.{name}"


def _text_cell(text: str) -> str:
    """A user-chosen name as a CSV cell a spreadsheet reads as text: a leading =, +, -, @,
    tab or carriage return gets a single quote in front so it is not evaluated."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _row(c: dict, rmap, peripheral_maps: Optional[Dict[int, Any]] = None) -> list:
    data = command_payload(c)
    crc = "" if not c.get("has_manager_packet") else ("OK" if c.get("crc_valid") else "BAD")
    return [
        c.get("start_sample", 0), c.get("end_sample", 0),
        c.get("command", ""), c.get("phase", ""),
        _devices(c.get("device_mask", 0)),
        f"0x{c['address']:08X}" if c.get("has_address") else "",
        _register_label(rmap, c, peripheral_maps),
        data.hex(" ") if data else "",
        crc, response_summary(c), command_error(c) or "",
    ]


def write_commands_csv(commands: List[dict], path: str, rmap=None,
                       peripheral_maps: Optional[Dict[int, Any]] = None) -> str:
    """The commands of one Link; `peripheral_maps` ({device: PeripheralRegisterMap}) names
    the registers outside the SWI3S map."""
    def write(f):
        w = csv.writer(f)
        w.writerow(_HEADER)
        for c in commands:
            w.writerow(_row(c, rmap, peripheral_maps))
    write_whole(path, write, mode="w", newline="", encoding="utf-8")
    return path


def write_links_commands_csv(links: Sequence[Tuple], path: str, rmap=None) -> str:
    """Every Link's commands in one CSV, in GLOBAL time order: `links` is one
    (name, commands, to_ps[, peripheral_maps]) per Link, `to_ps` mapping that Link's sample
    numbers to global picoseconds and `peripheral_maps` that Link's own device maps. Each
    row leads with its Link and its global start time; start_sample and end_sample stay in
    the Link's own samples, as in the one-Link export."""
    rows = []
    maps: List[Optional[Dict[int, Any]]] = []
    for order, link in enumerate(links):
        name, commands, to_ps = link[0], link[1], link[2]
        maps.append(link[3] if len(link) > 3 else None)
        for c in commands:
            rows.append((to_ps(int(c.get("start_sample", 0))), order, name, c))
    rows.sort(key=lambda r: (r[0], r[1]))            # time, then Link order on a tie
    def write(f):
        w = csv.writer(f)
        w.writerow(["link", "start_ps"] + _HEADER)
        for t_ps, order, name, c in rows:
            w.writerow([_text_cell(str(name)), t_ps] + _row(c, rmap, maps[order]))
    write_whole(path, write, mode="w", newline="", encoding="utf-8")
    return path
