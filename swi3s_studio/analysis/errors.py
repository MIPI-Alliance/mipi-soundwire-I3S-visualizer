"""Decode-quality analysis: per-command error classification."""
from __future__ import annotations

from typing import Iterable, List, Optional, Tuple

from ..model.registers import DP_BASE, DP_STRIDE
from . import port_status

_SINGLE_DEVICE_PHASES = {"Write", "ReadSetup", "ReadData", "CalibratePhy"}
# Peripheral Response error tokens ("Assignment of Robust Token Numbers (Peripheral)" table).
_PERIPH_ERRORS = {12: "PROTOCOL_ERROR", 13: "COMMAND_ERROR",
                  14: "TRANSPORT_ERROR", 15: "COMMANDS_BLOCKED"}


def _device_count(mask: int) -> int:
    return bin(mask & 0xFFF).count("1")


_ENABLECH_CURR_MSG = "Write to EnableCh_CURR with Interval != 1 Row"
_UNEXPECTED_SSP_MSG = "SSP mid skip pattern (accumulated skipping != 0)"
READ_ONLY_WRITE_MSG = "Programming error: write to a read-only register"
# Single-ranked data-port control registers, by offset in a port's block.
_RUNNING_PORT_REGS = {0x09: "SampleSizeGrouping", 0x0B: "PortControl",
                      0x0C: "SkippingNumerator", 0x0D: "SkippingNumerator",
                      0x0E: "FlowControl"}


def _running_port_write_msg(cmd: dict) -> str:
    """'Programming error: <register> of DP<n> changed while the port runs'. The decoder
    flags the write without saying which byte changed, so the message names the
    single-ranked control register the write covers when there is exactly one, and
    otherwise lists every one it covers ('one of DP0.PortControl, DP0.FlowControl')."""
    addr = int(cmd.get("address", 0))
    covered: List[Tuple[int, str]] = []
    for a in range(addr, addr + max(1, len(cmd.get("data") or b""))):
        off = (a - DP_BASE) % DP_STRIDE
        if a >= DP_BASE and off in _RUNNING_PORT_REGS:
            pair = ((a - DP_BASE) // DP_STRIDE, _RUNNING_PORT_REGS[off])
            if pair not in covered:
                covered.append(pair)
    if not covered:
        return "Programming error: a running port's control register changed"
    if len(covered) == 1:
        port, reg = covered[0]
        return f"Programming error: {reg} of DP{port} changed while the port runs"
    names = ", ".join(f"DP{port}.{reg}" for port, reg in covered)
    return f"Programming error: one of {names} changed while the port runs"


def command_error(cmd: dict) -> Optional[str]:
    """Return a short error label for a command, or None if it looks clean.

    Flags CRC failures, an unreadable command header (`header_valid` false: the row
    carries no device mask or phase to judge), device-mask cardinality (Section 8.1.2.2:
    Write/Read/CalibratePhy must select exactly one device; other commands must
    select at least one), a commit with no commit-group selected (it would commit
    nothing), peripheral error-response tokens, a decoder-flagged
    EnableCh_CURR write with Interval != 1 Row (native Decoder::feed /
    CommandRec::enablechCurrError — unsafe per the SWI3S spec), an SSP-generating
    command whose SSP landed while a Payload Interval Skipping port was part-way through
    its skip pattern (Section 9.1.6.2.1 — CommandRec::unexpectedSspError), a Read
    whose reply carried a peripheral-reported fault or ImpDef/SDCA interrupt (see
    :mod:`.port_status`), and a Write that changed a single-ranked control register of a
    running port (CommandRec::runningPortWriteError) — a programming error, not a
    protocol error: those registers take effect at once and are set while the port's
    channels are disabled — and a Write that covered a read-only byte
    (CommandRec::readOnlyWriteError: a register of access RO, or a CDS _CURR alias), a
    programming error that leaves the byte unchanged.
    """
    if cmd.get("enablech_curr_error"):
        return _ENABLECH_CURR_MSG
    if cmd.get("unexpected_ssp_error"):
        return _UNEXPECTED_SSP_MSG
    if cmd.get("running_port_write_error"):
        return _running_port_write_msg(cmd)
    if cmd.get("read_only_write_error"):
        return READ_ONLY_WRITE_MSG
    if cmd.get("has_manager_packet") and not cmd.get("crc_valid"):
        return "CRC error"
    if cmd.get("header_valid") is False:
        return "header unreadable"
    reported = port_status.fault_label(cmd)
    if reported:
        return reported
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
    # tokens), not the scalar peripheral_response; flag those too.
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
