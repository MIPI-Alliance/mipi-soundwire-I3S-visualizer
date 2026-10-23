"""Every bus access to one register: when, what value, and whether it took.

`register_history(commands, device, address)` lists, in capture order, each command that wrote
or read the byte at `address` on `device`: a WriteA32 whose data covers it, a ReadA32 whose
returned data covers it, a deferred read's ReadData delivery, and the attempts that did not
land — a CRC-failed packet, a peripheral that answered WRITE_FAILED, READ_FAILED or an error
token, or did not answer at all. The Registers pane shows it per row (right-click ▸ History…).

"Applied" follows the register model (model.registers.apply_commands): a CRC-valid WriteA32
whose device mask selects exactly one device (Section 8.1.2.2) is applied whatever the
peripheral answered, and a read is applied when its returned data passed its CRC. An applied
write can still carry WRITE_FAILED; the event shows both. A WriteA32 that selects more than
one device is listed, not applied, and its status names the mask error.

A failed read carries no data, so it is matched at its own start address only. A CRC-failed
packet is matched at the address it decoded to, which may be wrong; its status says so.

A ReadData that did not deliver data carries no address of its own. It is matched at the
start address of the device's pending deferred read: the last ReadA32 answered
REMOTE_READ_DEFERRED, until a ReadData delivers data for it.

A dual-ranked register's _CURR alias takes its value from a Commit, or from a write to the
_CURR alias itself. For such an address the history also lists the accesses to its _NEXT
alias (access "Write (NEXT)", "Read (NEXT)", …) and each Commit to the device whose group mask
covers the register's commit group (access "Commit", value the staged _NEXT byte it
promotes). A data port's commit group is its CommitGroupMemb as the bus left it before that
Commit. A write to the _CURR alias sets _NEXT too (Dual-Curr-is-RW, "Register Access Type"
table), so the history of a _NEXT alias also lists the writes to its _CURR alias (access
"Write (CURR)"), and a later Commit promotes that value. A CDS register's _CURR alias is
read-only (Dual-CURR-RO): a write to it is listed and changes nothing.

A Commit promotes the devices its device mask selects, none when the mask is 0 (Section
8.1.1.5.1, "Commit Phase for SSCR and DSCR Commands" table), as the register model does. A
bus reset (`resets`, the samples where a later Cold Start's Bus Reset begins) returns the
staged state to the register reset values and drops the pending deferred read: a command
that starts at or after a reset sees only what the bus carried since it.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Dict, Iterable, List, Optional

from ..model.registers import (
    CURR_RANK_OFFSET,
    DP_BASE,
    DP_COMMIT_GROUP_OFFSET,
    DP_STRIDE,
    DeviceRegisterFile,
    Provenance,
    RegisterMap,
    selects_one_device,
)
from .errors import command_error
from .responses import peripheral_response_name, response_summary

_READS = ("ReadA32", "ReadData")
# Peripheral answers that mean the access did not happen as asked. REMOTE_WRITE_BUFFERED and
# REMOTE_READ_DEFERRED are not failures: the access completes later.
_FAILED = {"WRITE_FAILED", "READ_FAILED", "REMOTE_WRITE_BUSY", "REMOTE_ACCESS_DISABLED",
           "PROTOCOL_ERROR", "COMMAND_ERROR", "TRANSPORT_ERROR", "COMMANDS_BLOCKED"}
_DEFERRED = "REMOTE_READ_DEFERRED"
_CG0 = 0x01                     # SLC/CDS/PHY dual-ranked registers are Commit Group 0


@dataclass
class RegisterEvent:
    command_index: int          # index into the session's command list
    start_sample: int
    bus_row: int
    access: str                 # "Write", "Read", "Read (deferred delivery)" or "Commit";
                                # "NEXT" in the brackets marks a _CURR register's _NEXT alias,
                                # "CURR" a _NEXT register's _CURR alias
    value: Optional[int]        # the byte at the address, when the command carried it
    applied: bool               # the register model took it (see the module docstring)
    status: str                 # "OK", or what went wrong
    response: str               # the command's response cell, as the Commands table shows

    @property
    def failed(self) -> bool:
        return self.status != "OK"


@lru_cache(maxsize=1)
def _default_map() -> RegisterMap:
    return RegisterMap.load()


def _status(cmd: dict, access: str) -> str:
    problems = []
    if cmd.get("has_manager_packet") and not cmd.get("crc_valid"):
        problems.append("CRC error: address and data unreliable")
    if (not access.startswith("Write") and cmd.get("has_read_data")
            and not cmd.get("read_data_crc_valid")):
        problems.append("read data CRC error")
    pr = int(cmd.get("peripheral_response", -1))
    name = peripheral_response_name(cmd.get("phase", ""), pr) if pr >= 0 else ""
    if pr < 0 and cmd.get("phase") in ("Write", "ReadSetup", "ReadData"):
        problems.append("no response")
    elif name in _FAILED:
        problems.append(name)
    if access == "Commit" and not cmd.get("commit_confirmed"):
        problems.append(name if name == "COMMIT_NOT_READY" else "not confirmed")
    other = command_error(cmd)
    if other and other != "CRC error" and all(other not in p for p in problems):
        problems.append(other)
    return "; ".join(problems) or "OK"


def _rank_delta(rmap: RegisterMap, address: int, rank: str) -> Optional[int]:
    """The _CURR address minus the _NEXT address of the dual-ranked register whose `rank`
    alias is `address`, or None when `address` is no such alias."""
    res = rmap.resolve(address)
    if res is None or not res.register.dual_ranked or res.rank != rank:
        return None
    spec = res.register
    return spec.curr_offset - spec.offset if spec.curr_offset is not None else CURR_RANK_OFFSET


def _next_alias(rmap: RegisterMap, address: int) -> Optional[int]:
    """The _NEXT address of the dual-ranked register whose _CURR alias is `address`, or None."""
    delta = _rank_delta(rmap, address, "CURR")
    return None if delta is None else address - delta


def _curr_alias(rmap: RegisterMap, address: int) -> Optional[int]:
    """The _CURR address of the dual-ranked register whose _NEXT alias is `address`, or None."""
    delta = _rank_delta(rmap, address, "NEXT")
    return None if delta is None else address + delta


def _commit_group(rmap: RegisterMap, staged: DeviceRegisterFile, next_addr: int) -> int:
    """The commit-group mask of the register at `next_addr`, as `staged` holds the bus state."""
    res = rmap.resolve(next_addr)
    if res is None or res.dp_index is None:
        return _CG0
    addr = DP_BASE + res.dp_index * DP_STRIDE + DP_COMMIT_GROUP_OFFSET
    if staged.provenance(addr) != Provenance.DEFAULT:
        return staged.value(addr)
    return staged.value(addr) or _CG0


def _access(op: str, rank: str) -> str:
    notes = ([rank] if rank else []) + (["deferred delivery"] if op == "ReadData" else [])
    kind = "Write" if op == "WriteA32" else "Read"
    return f"{kind} ({', '.join(notes)})" if notes else kind


def _event(i: int, cmd: dict, access: str, value: Optional[int], applied: bool) -> RegisterEvent:
    return RegisterEvent(
        command_index=i, start_sample=int(cmd.get("start_sample", 0)),
        bus_row=int(cmd.get("bus_row", 0)), access=access, value=value,
        applied=applied, status=_status(cmd, access), response=response_summary(cmd))


def register_history(commands: List[dict], device: int, address: int,
                     register_map: Optional[RegisterMap] = None,
                     resets: Iterable[int] = ()) -> List[RegisterEvent]:
    """Each write or read of `address` on `device`, in capture order (see the module doc).

    `register_map` resolves a _CURR alias to its _NEXT address and commit group; the SWI3S
    map from data/registers.json when not given. `resets` are the bus-reset samples
    (Session.bus_reset_samples())."""
    bit = 1 << int(device)
    address = int(address)
    rmap = register_map or _default_map()
    next_addr = _next_alias(rmap, address)
    curr_addr = _curr_alias(rmap, address)
    targets: Dict[int, str] = {address: ""}           # address -> the other alias's rank
    staged: Optional[DeviceRegisterFile] = None
    if next_addr is not None:
        targets[next_addr] = "NEXT"
        staged = DeviceRegisterFile(rmap, int(device))
    elif curr_addr is not None:
        targets[curr_addr] = "CURR"                   # its writes set _NEXT too
    pending: Optional[int] = None          # start address of the device's deferred read
    reset_at = sorted(int(r) for r in resets)
    out: List[RegisterEvent] = []
    for i, cmd in enumerate(commands):
        while reset_at and int(cmd.get("start_sample", 0)) >= reset_at[0]:
            reset_at.pop(0)
            pending = None
            if staged is not None:
                staged = DeviceRegisterFile(rmap, int(device))
        op = cmd.get("command")
        mask = int(cmd.get("device_mask", 0))
        if cmd.get("is_commit"):
            if staged is not None and next_addr is not None and mask & bit:
                if _commit_group(rmap, staged, next_addr) & int(cmd.get("group_mask", 0)):
                    has_staged = staged.provenance(next_addr) != Provenance.DEFAULT
                    out.append(_event(i, cmd, "Commit",
                                      staged.value(next_addr) if has_staged else None,
                                      bool(cmd.get("commit_confirmed")) and has_staged))
            continue
        if (op != "WriteA32" and op not in _READS) or not mask & bit:
            continue
        if cmd.get("has_address"):
            base: Optional[int] = int(cmd.get("address", -1))
        else:
            base = pending if op == "ReadData" else None
        if op == "ReadA32" and base is not None:
            pr = int(cmd.get("peripheral_response", -1))
            if peripheral_response_name("ReadSetup", pr) == _DEFERRED:
                pending = base
        elif op == "ReadData" and cmd.get("has_read_data"):
            pending = None
        if base is None:
            continue
        if op == "WriteA32":
            payload = bytes(cmd.get("data") or b"")
            applied = bool(cmd.get("crc_valid")) and selects_one_device(mask)
        else:
            payload = bytes(cmd.get("read_data") or b"") if cmd.get("has_read_data") else b""
            applied = bool(payload) and bool(cmd.get("read_data_crc_valid"))
        if staged is not None and applied:
            if op == "WriteA32":
                staged.apply_write(base, payload)
            else:
                staged.apply_read(base, payload)
        span = len(payload) or 1
        for target, rank in targets.items():
            if rank == "CURR" and op != "WriteA32":
                continue                              # a _CURR read leaves _NEXT as it is
            if base <= target < base + span:
                out.append(_event(i, cmd, _access(op, rank),
                                  payload[target - base] if payload else None, applied))
    return out
