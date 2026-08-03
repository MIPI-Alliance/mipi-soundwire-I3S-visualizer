"""Per-symbol meaning for the CDS 8b/10b stream.

The Control Data Stream carries the Command Transport Protocol (spec §8.1): a
K.28.7 SPM, then a 6-token Phase Header (PhaseID, 12-bit Device Mask, 8-bit
Packet Length), then — if Packet Length > 0 — the Manager Packet (opcode +
fields + CRC16), then the per-phase response slots, then a Protocol Spacer.

Given the decoded symbol stream (one dict per symbol, as produced by
swi3score.decode_symbols), `annotate()` labels each symbol with its role in that
grammar — e.g. "PhaseID: GET_STATUS", "Device Mask_H = 0x3", "Opcode: WRITEA32",
"Address[31:24] = 0x40", "Manager_CRC16_H = 0x1A", "Peripheral Response: WRITE_OK".
Data-carrying fields include the decoded nibble/byte value.

This mirrors CCommandTransportParser's state machine but is display-only and runs
on the windowed symbol list. It re-syncs on every comma, so a window that starts
mid-stream simply shows blanks until the first SPM, then labels normally.

Symbol dict fields used: kind (0 invalid,1 comma,2 robust token,3 D-code,
4 K-code) and value (token number for kind 2; byte for kind 3/4; else -1).
"""
from __future__ import annotations

from typing import List

_KIND_COMMA = 1
_KIND_RT = 2
_KIND_NORESP = 5     # undriven all-ones response field (0x3FF)
# Between commands the Manager drives the alternating idle filler (…101010…); the two
# 10-bit phases of that pattern are 0x155 (0101010101) and 0x2AA (1010101010). Only an
# idle-pattern label between phases — the SAME codeword is a legitimate MP/PM spacer
# inside a command, so this is scoped to the hunt/idle states.
_IDLE_ALT = frozenset({0x155, 0x2AA})


def _resp_name(kind: int, phase: int, token: int) -> str:
    """Peripheral-response name, treating the undriven all-ones symbol as
    NO_RESPONSE (it carries no robust token)."""
    if kind == _KIND_NORESP:
        return "NO_RESPONSE"
    return _peripheral_response(phase, token)

_SYMBOL_BITS = 10
_HEADER_TOKENS = 6
_MAX_PERIPHERALS = 12
_SPACER_MP = _SYMBOL_BITS // _SYMBOL_BITS          # 10-bit Manager->Peripheral = 1 symbol
_SPACER_PM = 20 // _SYMBOL_BITS                    # 20-bit Peripheral->Manager (default) = 2

_PHASE_NAMES = {
    0: "GET_STATUS", 1: "WRITE", 2: "READ_SETUP", 3: "READ_DATA",
    4: "COMMIT", 5: "ANNOUNCE", 6: "CALIBRATE_PHY",
}
_OPCODES = {
    0: {0: "PING"},
    1: {0: "WRITEA32", 1: "UNBLOCK"},
    2: {0: "READA32"},
    4: {0: "SSCR", 1: "DSCR"},
    5: {0: "SSPA", 1: "EXIT_DORMANT"},
    6: {0: "INIT_CAL", 1: "TRIM_CAL"},
}
_ADDR_BITS = [(31, 24), (23, 16), (15, 8), (7, 0)]


def _opcode_name(phase: int, op: int) -> str:
    return _OPCODES.get(phase, {}).get(op, f"0x{op & 0xFF:02X}")


def _peripheral_response(phase: int, token: int) -> str:
    """Peripheral Response token name, resolved within the phase (Table 67)."""
    if token < 0:
        return "?"
    if token == 0:
        return {1: "WRITE_OK", 2: "READ_DATA_NOW", 3: "READ_DATA_NOW",
                4: "COMMIT_READY", 6: "CALIBRATE_NOW",
                0: "PING_ATTACHED"}.get(phase, "RT0")
    if token == 2:
        return "COMMIT_NOT_READY" if phase == 4 else "RT2"
    if token == 4:
        return "WRITE_FAILED" if phase == 1 else ("READ_FAILED" if phase in (2, 3) else "RT4")
    if token == 5:
        return "REMOTE_WRITE_BUFFERED" if phase == 1 else (
            "REMOTE_READ_DEFERRED" if phase in (2, 3) else "RT5")
    if token == 6:
        return "PING_ATTACHED_BUSY" if phase == 0 else "REMOTE_WRITE_BUSY"
    if token == 7:
        return "REMOTE_ACCESS_DISABLED"
    if token == 8:
        return "PING_ALERT" if phase == 0 else "RT8"
    if token == 10:
        return "PING_ALERT_BUSY" if phase == 0 else "RT10"
    if token == 12:
        return "PROTOCOL_ERROR"
    if token == 13:
        return "COMMAND_ERROR"
    if token == 14:
        return "TRANSPORT_ERROR"
    if token == 15:
        return "COMMANDS_BLOCKED"
    return f"RT{token}"


def _manager_response(phase: int, token: int) -> str:
    if token == 0:
        return "CONFIRM_COMMIT" if phase == 4 else "READ_DATA_OK"
    if token == 15:
        return "CANCEL_COMMIT" if phase == 4 else "READ_DATA_ERROR"
    return f"RT{token}" if token >= 0 else "?"


def _packet_field(phase: int, opcode: int, idx: int, pktlen: int) -> str:
    """Label for Manager Packet byte `idx` (0-based), pktlen excludes the CRC16.
    The Manager Packet CRC is labelled Manager_CRC16 to distinguish it from the
    peripheral Read-Data CRC (see _st_read_data)."""
    if idx == pktlen:
        return "Manager_CRC16_H"
    if idx == pktlen + 1:
        return "Manager_CRC16_L"
    if idx == 0:
        return f"Opcode: {_opcode_name(phase, opcode)}"
    if phase == 1 and opcode == 0:                 # WRITEA32
        if 1 <= idx <= 4:
            hi, lo = _ADDR_BITS[idx - 1]
            return f"Address[{hi}:{lo}]"
        return f"Write Data[{idx - 5}]"
    if phase == 2 and opcode == 0:                 # READA32
        if idx == 1:
            return "Read Byte Count"
        if 2 <= idx <= 5:
            hi, lo = _ADDR_BITS[idx - 2]
            return f"Address[{hi}:{lo}]"
        return f"MP Byte {idx}"
    if phase in (4, 5):                            # COMMIT / ANNOUNCE
        if idx == 1:
            return "Group Mask"
        if idx == 2:
            return "Row Delay"
        return f"MP Byte {idx}"
    return f"MP Byte {idx}"


class _Annotator:
    """Walks the symbol stream, tracking the Command Transport phase grammar."""

    def __init__(self) -> None:
        self.state = "hunt"
        self.phase = 0
        self.opcode = 0
        self.pktlen = 0
        self.hidx = 0
        self.pidx = 0
        self.count = 0          # generic countdown (spacer/response slots)
        self.dev = 0            # device index for per-device response slots
        self.read_bytes = 0     # remaining read-data bytes (ReadSetup-with-data)
        self._rd_idx = 0        # index within the Read Data field
        self._after = "idle"    # state to enter when the current spacer ends
        self.raw = -1           # last symbol's raw 10-bit codeword (idle-pattern detect)

    def feed(self, sym: dict) -> str:
        kind = sym.get("kind", 0)
        val = sym.get("value", -1)
        self.raw = int(sym.get("raw", -1)) & 0x3FF     # for idle-pattern detection

        if kind == _KIND_COMMA:                    # SPM resets the phase, anywhere
            self.state = "header"
            self.hidx = 0
            return "SPM (Start of Phase)"

        handler = getattr(self, f"_st_{self.state}", None)
        return handler(kind, val) if handler else ""

    # -- states -----------------------------------------------------------
    def _st_hunt(self, kind, val) -> str:
        # Pre-sync: waiting for a comma. Between commands the Manager drives the
        # alternating idle filler — label it rather than leaving the row blank.
        return "Idle Pattern" if self.raw in _IDLE_ALT else ""

    def _st_idle(self, kind, val) -> str:
        # Phase done; idle until the next SPM. The alternating filler here is the
        # bus idle pattern (the same codeword mid-phase is a spacer — see _IDLE_ALT).
        return "Idle Pattern" if self.raw in _IDLE_ALT else ""

    def _st_header(self, kind, val) -> str:
        i = self.hidx
        self.hidx += 1
        v = val if (val is not None and val >= 0) else 0
        if i == 0:
            self.phase = v
            return f"PhaseID: {_PHASE_NAMES.get(self.phase, '?')}"
        if i in (1, 2, 3):
            return f"Device Mask_{'HML'[i - 1]} = 0x{v:X}"     # 4-bit nibble
        if i == 4:
            self._plen_hi = v & 0xF                # Packet Length high nibble
            return f"Packet Length_H = 0x{v:X}"
        # i == 5: low nibble completes Packet Length; decide what follows.
        self.pktlen = (getattr(self, "_plen_hi", 0) << 4) | (v & 0xF)
        self._begin_after_header()
        return f"Packet Length_L = 0x{v:X}  (len {self.pktlen})"

    def _begin_after_header(self) -> None:
        if self.pktlen > 0:
            self.state = "packet"
            self.pidx = 0
        elif self.phase == 3:                      # ReadData: MP spacer -> response
            self._goto("spacer", _SPACER_MP, "read_resp")
        else:
            self.state = "idle"                    # zero-length phase ends

    def _st_packet(self, kind, val) -> str:
        idx = self.pidx
        self.pidx += 1
        if idx == 0:
            self.opcode = val if val and val >= 0 else 0
        elif idx == 1 and self.phase == 2 and self.opcode == 0:   # READ_A32 byte count (excess-1)
            self.read_bytes = (val + 1) if (val is not None and val >= 0) else 0
        label = _packet_field(self.phase, self.opcode, idx, self.pktlen)
        if idx != 0 and val is not None and val >= 0:   # opcode keeps its name; else show the byte
            label = f"{label} = 0x{(val & 0xFF):02X}"
        if self.pidx == self.pktlen + 2:           # opcode + fields + CRC16 done
            self._after_packet()
        return label

    def _after_packet(self) -> None:
        if self.phase == 0:                        # Ping: spacer -> 12 status tokens
            self._goto("spacer", _SPACER_MP, "ping_status")
        elif self.phase == 1:                      # Write: spacer -> 1 response
            self._goto("spacer", _SPACER_MP, "write_resp")
        elif self.phase == 2:                      # ReadSetup: spacer -> response
            self._goto("spacer", _SPACER_MP, "read_resp")
        elif self.phase == 4:                      # Commit: spacer -> 12 resp -> ...
            self._goto("spacer", _SPACER_MP, "commit_periph")
        else:                                      # Announce / CalibratePhy / Unblock
            self.state = "idle"

    def _goto(self, state, count, after) -> None:
        self.state = state
        self.count = count
        self._after = after

    def _st_spacer(self, kind, val) -> str:
        self.count -= 1
        if self.count <= 0:
            self.state = self._after
            if self.state in ("ping_status", "commit_periph"):
                self.dev = 0
        return "Spacer"

    def _st_ping_status(self, kind, val) -> str:
        dev = self.dev
        self.dev += 1
        if self.dev >= _MAX_PERIPHERALS:
            self.state = "idle"
        return f"Ping Status Dev {dev}: {_resp_name(kind, 0, val)}"

    def _st_write_resp(self, kind, val) -> str:
        self.state = "idle"
        return f"Peripheral Response: {_resp_name(kind, self.phase, val)}"

    def _st_read_resp(self, kind, val) -> str:
        name = _resp_name(kind, self.phase, val)
        if kind == _KIND_RT and val == 0 and self.read_bytes > 0:   # READ_DATA_NOW: data follows
            self._rd_idx = 0
            self._goto("spacer", _SPACER_MP, "read_data")
        else:
            self.state = "idle"
        return f"Peripheral Response: {name}"

    def _st_read_data(self, kind, val) -> str:
        # `read_bytes` data bytes, then CRC16 H/L, then a PM spacer + mgr response.
        # INVARIANT: entered only with read_bytes >= 1 — the byte count is excess-1
        # encoded (val+1 at line ~202, so a valid count is >= 1), and _st_read_resp
        # only transitions here when read_bytes > 0. So the first call always takes
        # the data branch and sets count=2 before the CRC branch runs; the CRC path
        # can't be reached with a stale count==0.
        if self.read_bytes > 0:
            label = f"Read Data[{self._rd_idx}] = 0x{(val & 0xFF):02X}" if (val is not None and val >= 0) else f"Read Data[{self._rd_idx}]"
            self._rd_idx += 1
            self.read_bytes -= 1
            if self.read_bytes == 0:
                self.count = 2                     # two CRC16 bytes follow
            return label
        self.count -= 1                            # CRC16 H then L
        label = "Peripheral_CRC16_H" if self.count == 1 else "Peripheral_CRC16_L"
        if self.count <= 0:
            self._goto("spacer", _SPACER_PM, "read_mgr_resp")
        return f"{label} = 0x{(val & 0xFF):02X}" if (val is not None and val >= 0) else label

    def _st_read_mgr_resp(self, kind, val) -> str:
        self.state = "idle"
        return f"Manager Response: {_manager_response(self.phase, val)}"

    def _st_commit_periph(self, kind, val) -> str:
        dev = self.dev
        self.dev += 1
        if self.dev >= _MAX_PERIPHERALS:
            self._goto("spacer", _SPACER_PM, "commit_mgr_resp")
        return f"Commit Resp Dev {dev}: {_resp_name(kind, 4, val)}"

    def _st_commit_mgr_resp(self, kind, val) -> str:
        self.state = "idle"
        return f"Manager Response: {_manager_response(4, val)}"


def annotate(symbols: List[dict]) -> List[str]:
    """Return a list of per-symbol meaning strings, one per input symbol."""
    a = _Annotator()
    return [a.feed(s) for s in symbols]
