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
on the windowed symbol list. Each per-device response slot is one symbol: decode_symbols,
given the session's hub depths, frames a hub-delayed device's token after its gap bits, as
the parser reads it. It re-syncs on every comma, so a window that starts
mid-stream simply shows blanks until the first SPM, then labels normally. A Phase Header
holding a symbol that is not a Robust Token, or a PhaseID above 6, is a bad header, as
the core reads it: its six tokens are labelled, the last one marked "(Bad header)", and
the phase ends there with no packet or response labels.

Symbol dict fields used: kind (0 invalid, 1 comma, 2 robust token, 3 D-code, 4 K-code,
5 undriven all-ones response field), value (token number for kind 2; byte for kind 3/4;
else -1), and raw (the 10-bit codeword, read to label the idle pattern between
phases). A symbol without raw never gets an "Idle Pattern" label. A Manager Packet or
Read Data byte whose codeword is also a Robust Token (kind 2) shows the byte its 8b/10b
decode of raw gives, as the core's parser reads it; without raw it shows the value.
"""
from __future__ import annotations

import functools
import re
from typing import List

from . import responses

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

# PhaseID -> the phase name the Commands table and `responses` use (the core's spelling).
_RESPONSE_PHASE = {0: "GetStatus", 1: "Write", 2: "ReadSetup", 3: "ReadData",
                   4: "Commit", 5: "Announce", 6: "CalibratePhy"}
# The symbol pane shows the specification's upper-case protocol names, derived from the
# same table so a phase is defined once.
_PHASE_NAMES = {k: re.sub(r"(?<!^)(?=[A-Z])", "_", v).upper()
                for k, v in _RESPONSE_PHASE.items()}
# Upper-case protocol names, matching the core's command names up to case and underscores.
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
    return _OPCODES.get(phase, {}).get(op, f"Op0x{op & 0xFF:02X}")


def _peripheral_response(phase: int, token: int) -> str:
    """Peripheral Response token name, resolved within the phase by
    `responses.peripheral_response_name` (the one table, shared with the Commands table
    and the CSV export)."""
    if token < 0:
        return "?"
    return responses.peripheral_response_name(_RESPONSE_PHASE.get(phase, ""), token)


def _manager_response(phase: int, token: int) -> str:
    """Manager Response token name from `responses.manager_response_name`; a token the
    table leaves unnamed shows as its number."""
    if token < 0:
        return "?"
    return responses.manager_response_name(_RESPONSE_PHASE.get(phase, ""), token) \
        or f"RT{token}"


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


@functools.lru_cache(maxsize=1024)
def _dcode_byte(raw: int) -> int:
    """The data byte 10-bit codeword `raw` decodes to, or -1 when it is not a D-code."""
    import swi3score
    d = swi3score.decode_codeword(raw & 0x3FF)
    return int(d["value"]) if d["valid"] and not d["is_control"] else -1


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
        self.header_ok = True   # every Phase Header token so far read, PhaseID defined
        self.raw = -1           # last symbol's raw 10-bit codeword (idle-pattern detect)
        self.codeword = None    # last symbol's raw, None when it carries none

    def feed(self, sym: dict) -> str:
        kind = sym.get("kind", 0)
        val = sym.get("value", -1)
        self.raw = int(sym.get("raw", -1)) & 0x3FF     # for idle-pattern detection
        self.codeword = sym.get("raw")

        if kind == _KIND_COMMA:                    # SPM resets the phase, anywhere
            self.state = "header"
            self.hidx = 0
            self.header_ok = True
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
        readable = kind == _KIND_RT and val is not None and val >= 0
        v = val if readable else 0
        if not readable:
            self.header_ok = False
        if i == 0:
            self.phase = v
            if not readable:
                return "PhaseID: unreadable"
            if v not in _PHASE_NAMES:
                self.header_ok = False
                return f"PhaseID: undefined ({v})"
            return f"PhaseID: {_PHASE_NAMES[v]}"
        if i in (1, 2, 3):
            field = f"Device Mask_{'HML'[i - 1]}"
            return f"{field} = 0x{v:X}" if readable else f"{field}: unreadable"   # 4-bit nibble
        if i == 4:
            self._plen_hi = v & 0xF                # Packet Length high nibble
            return f"Packet Length_H = 0x{v:X}" if readable else "Packet Length_H: unreadable"
        # i == 5: low nibble completes Packet Length; decide what follows.
        self.pktlen = (getattr(self, "_plen_hi", 0) << 4) | (v & 0xF)
        if not self.header_ok:
            self.state = "idle"                    # the core re-hunts the next comma
            label = (f"Packet Length_L = 0x{v:X}" if readable
                     else "Packet Length_L: unreadable")
            return f"{label}  (Bad header)"
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

    def _byte(self, kind, val):
        """The byte a Manager Packet or Read Data symbol carries: a Robust Token's codeword
        read as the D-code it also is, any other kind's value."""
        if kind == _KIND_RT and self.codeword is not None:
            return _dcode_byte(int(self.codeword))
        return val

    def _st_packet(self, kind, val) -> str:
        val = self._byte(kind, val)
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
        # INVARIANT: entered only with read_bytes >= 1 (the byte count is excess-1
        # encoded in _st_packet, and _st_read_resp transitions here only when
        # read_bytes > 0). The first call takes the data branch and sets count=2
        # before the CRC branch runs.
        val = self._byte(kind, val)
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
