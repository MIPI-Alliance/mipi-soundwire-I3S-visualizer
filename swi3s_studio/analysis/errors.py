"""Decode-quality analysis: per-command error classification."""
from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

_SINGLE_DEVICE_PHASES = {"Write", "ReadSetup", "ReadData", "CalibratePhy"}
# Peripheral Response error tokens (Table 67).
_PERIPH_ERRORS = {12: "PROTOCOL_ERROR", 13: "COMMAND_ERROR",
                  14: "TRANSPORT_ERROR", 15: "COMMANDS_BLOCKED"}


def _device_count(mask: int) -> int:
    return bin(mask & 0xFFF).count("1")


_ENABLECH_CURR_MSG = "Write to EnableCh_CURR with Interval != 1 Row"


def command_error(cmd: dict) -> Optional[str]:
    """Return a short error label for a command, or None if it looks clean.

    Flags CRC failures, device-mask cardinality (Section 8.1.2.2:
    Write/Read/CalibratePhy must select exactly one device; other commands must
    select at least one), a commit with no commit-group selected (it would commit
    nothing), peripheral error-response tokens, and a decoder-flagged
    EnableCh_CURR write with Interval != 1 Row (native Decoder::feed /
    CommandRec::enablechCurrError — unsafe per the SWI3S spec).
    """
    if cmd.get("enablech_curr_error"):
        return _ENABLECH_CURR_MSG
    if cmd.get("has_manager_packet") and not cmd.get("crc_valid"):
        return "CRC error"
    phase = cmd.get("phase", "")
    n = _device_count(cmd.get("device_mask", 0))
    if phase in _SINGLE_DEVICE_PHASES:
        if n != 1:
            return f"device mask: expected 1, got {n}"
    elif n == 0:
        return "device mask: no devices selected"
    if cmd.get("is_commit"):
        gm = int(cmd.get("group_mask", 0))
        if not gm:
            return "group mask: no commit groups selected"
        # Only CG0..CG3 exist (bits [3:0]); bits 4+ are reserved and must be 0. A commit
        # with a reserved bit set (e.g. 0xFF) is malformed.
        if gm & ~0x0F:
            return f"group mask: reserved bits set (0x{gm:02X})"
    pr = cmd.get("peripheral_response", -1)
    if pr in _PERIPH_ERRORS:
        return _PERIPH_ERRORS[pr]
    # GetStatus/Ping error responses land in the per-device ping_status list (12
    # tokens), NOT the scalar peripheral_response — flag those too, or count_errors
    # under-reports a peripheral reporting an error to a status poll.
    ping = cmd.get("ping_status")
    if ping:
        for dev, tok in enumerate(ping):
            if tok in _PERIPH_ERRORS:
                return f"{_PERIPH_ERRORS[tok]} (dev {dev})"
    return None


def is_error(cmd: dict) -> bool:
    return command_error(cmd) is not None


def count_errors(commands: Iterable[dict]) -> int:
    return sum(1 for c in commands if is_error(c))


def error_rows(commands: List[dict]) -> List[Tuple[int, str]]:
    """(row index, label) for every flagged command."""
    return [(i, command_error(c)) for i, c in enumerate(commands) if is_error(c)]
