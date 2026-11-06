"""Link-control events: PM_Action (link state-machine) changes snooped from the
bus. Link/PHY control in SWI3S is done via WriteA32 to specific registers + a
Commit — there are no dedicated opcodes. PHY *selection* is not on the CDS and
not in any register (there is no ActivePHY field); it is Cold Start physical-layer
signaling on the raw DP/DN lines (spec §5.1.2) and IS recoverable there — see
:mod:`swi3s_studio.analysis.link_control`, which decodes it from the capture's
edges. This module surfaces the one register that drives link-state transitions:
SLC PM_Action (0x1011)."""
from __future__ import annotations

from typing import List

PM_ACTION_ADDR = 0x1011        # SLC offset 0x11: Peripheral Management Action


def link_events(commands, rmap) -> List[dict]:
    """WriteA32s that cover SLC PM_Action, decoded to their action name (Stay_Attached /
    Enter_Dormant / Enter_Sleeping / …). A write that starts below PM_Action and runs
    through it counts. Each event: action, value, device_mask, bus_row, start_sample.
    The action takes effect on the next SSCR/DSCR Commit."""
    out: List[dict] = []
    res = rmap.resolve(PM_ACTION_ADDR) if rmap is not None else None
    for c in commands:
        if not (c.get("command") == "WriteA32" and c.get("crc_valid") and c.get("has_address")):
            continue
        data = c.get("data") or b""
        index = PM_ACTION_ADDR - int(c.get("address", 0))
        if not 0 <= index < len(data):
            continue
        byte = data[index]
        action = f"0x{byte & 0xF:X}"
        if res:
            for name, val, fld in res.register.decode(byte):
                if name == "PM_Action":
                    action = fld.value_label(val)
        out.append({"action": action, "value": byte & 0xF,
                    "device_mask": c.get("device_mask", 0),
                    "bus_row": c.get("bus_row", 0),
                    "start_sample": c.get("start_sample", 0)})
    return out
