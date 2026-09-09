"""SWI3S register model: the spec map + per-device register state.

Loads ``data/registers.json`` (the Raven-extracted source of truth) into a
:class:`RegisterMap` that can resolve a 32-bit WriteA32/ReadA32 address to its
block / register / field decode, and a :class:`DeviceRegisterFile` that tracks
each register's value AND provenance (DEFAULT reset value / WRITTEN on the wire /
CSV-imported / UI-modified) for the register-map view's colour coding.

Registers are byte-granular (each abs_address is one byte; fields are bit ranges
within it); a WriteA32 of N bytes writes consecutive addresses.
"""
from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple


def _default_registers_json() -> str:
    """Locate data/registers.json across dev, installed, and PyInstaller layouts."""
    env = os.environ.get("SWI3S_REGISTERS")
    if env and os.path.exists(env):
        return env
    here = os.path.dirname(os.path.dirname(__file__))            # swi3s_studio/
    candidates = [
        os.path.join(os.path.dirname(here), "data", "registers.json"),  # repo layout
        os.path.join(here, "data", "registers.json"),                   # packaged in pkg
        os.path.join(sys.prefix, "share", "swi3s-studio", "registers.json"),  # pip data-files
    ]
    base = getattr(sys, "_MEIPASS", None)                        # PyInstaller bundle
    if base:
        candidates.insert(0, os.path.join(base, "data", "registers.json"))
    for c in candidates:
        if os.path.exists(c):
            return c
    return candidates[0]


DEFAULT_REGISTERS_JSON = _default_registers_json()

SLC_BASE = 0x1000
DP_BASE = 0x2000
DP_STRIDE = 0x100
CURR_RANK_OFFSET = 0x40
# DP CommitGroupMemb (single-ranked, offset 0x0A, reset 0b0001 = Commit Group 0):
# a data port commits iff its membership mask intersects the commit's group mask.
DP_COMMIT_GROUP_OFFSET = 0x0A
# PHY electrical + CDS transport region bases (Table 164). One instance each.
PHY1_BASE = 0x100
PHY2_BASE = 0x200
PHY3_BASE = 0x300
CDS_BASE = 0x1100

# SWI3S peripheral address-region map (spec Tables 163 + 164). Used to label an
# address by its region when it isn't a register we model (e.g. device-defined
# space, PFP/Hub blocks). Ordered, non-overlapping, covering the full 32-bit space.
ADDRESS_REGIONS = [
    (0x00000000, 0x000000FF, "PHY0 (reserved)"),
    (0x00000100, 0x000001FF, "PHY1 (FBCSE)"),
    (0x00000200, 0x000002FF, "PHY2 (FBCSE)"),
    (0x00000300, 0x000003FF, "PHY3 (DLV)"),
    (0x00000400, 0x00000FFF, "PHY4-15 (reserved)"),
    (0x00001000, 0x000010FF, "System & Link Control"),
    (0x00001100, 0x000011FF, "Control Data Stream Transport"),
    (0x00001200, 0x00001FFF, "Reserved"),
    (0x00002000, 0x00003FFF, "Data Port Registers"),
    (0x00004000, 0x00005FFF, "Payload Forwarding Port"),
    (0x00006000, 0x00011FFF, "Reserved"),
    (0x00012000, 0x0001FFFF, "HubDFI1-7"),
    (0x00020000, 0x0FFFFFFF, "Reserved (SWI3S registers)"),
    (0x10000000, 0x3FFFFFFF, "Device-defined (Local)"),
    (0x40000000, 0x7FFFFFFF, "SDCA (Local)"),
    (0x80000000, 0x8FFFFFFF, "Reserved (Remote)"),
    (0x90000000, 0xBFFFFFFF, "Device-defined (Remote)"),
    (0xC0000000, 0xFFFFFFFF, "SDCA (Remote)"),
]


def region_name(address: int) -> str:
    """The Table 163/164 region an address falls in (e.g. 'Device-defined
    (Local)'), for labelling addresses outside the registers we model."""
    a = int(address) & 0xFFFFFFFF
    for lo, hi, name in ADDRESS_REGIONS:
        if lo <= a <= hi:
            return name
    return "Unknown"


class Provenance(Enum):
    DEFAULT = "default"   # reset value, never touched ("Cold Reset")
    WRITTEN = "written"   # set by a CRC-valid WriteA32 on the wire ("Bus Write")
    READ = "read"         # revealed by a CRC-valid Read's returned data ("Bus Read")
    CSV = "csv"           # imported from a visualizer CSV/JSON config ("CSV Import")
    UI = "ui"             # manually forced in the UI for what-if debug ("Manual Edit")


def _parse_int(value) -> Optional[int]:
    """Parse a reset/value token to an int, or None if not a plain number
    (e.g. 'ImpDef', 'see fields', PHY-dependent 'PHY2:1', '-')."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return None
    s = value.strip()
    try:
        if s.lower().startswith("0x"):
            return int(s, 16)
        if s.lower().startswith("0b"):
            return int(s, 2)
        return int(s, 10)
    except ValueError:
        return None


def _parse_offset_span(value) -> Tuple[Optional[int], int]:
    """An `offset` token to (first address, count of addresses it covers).

    The spec's register tables collapse runs of consecutive addresses into one row —
    "0x20-0x2F  EC_TestFailCh", "0x40-0x6F  [48 x Reserved]" — and this file mirrors that
    notation. Returns (None, 0) for a token that is neither a number nor a range, so the
    caller skips it rather than inventing an address.
    """
    single = _parse_int(value)
    if single is not None:
        return single, 1
    if not isinstance(value, str):
        return None, 0
    m = re.match(r"\s*(0[xX][0-9a-fA-F]+|\d+)\s*-\s*(0[xX][0-9a-fA-F]+|\d+)\s*$", value)
    if not m:
        return None, 0
    first, last = _parse_int(m.group(1)), _parse_int(m.group(2))
    if first is None or last is None or last < first:
        return None, 0
    return first, last - first + 1


def _parse_bits(bits: str) -> Tuple[int, int]:
    """'[7:5]' -> (7, 5); '[3]' -> (3, 3)."""
    m = re.match(r"\[(\d+)(?::(\d+))?\]", bits.strip())
    if not m:
        raise ValueError(f"bad bit range {bits!r}")
    hi = int(m.group(1))
    lo = int(m.group(2)) if m.group(2) is not None else hi
    return hi, lo


def _parse_enum(valid) -> Dict[int, str]:
    """Parse a field's `valid` string into a {value: name} map for symbolic
    decode, e.g. '0x0=Stay_Attached,0x4=Enter_Dormant' or 'b1001=v1.1'. Tokens
    without a parseable integer key (ranges like '0x02-0x0F=Rsvd', or plain
    '0-11') are skipped — only exact single-value labels are kept."""
    out: Dict[int, str] = {}
    if not isinstance(valid, str):
        return out
    for tok in valid.split(","):
        if "=" not in tok:
            continue
        key, name = tok.split("=", 1)
        key, name = key.strip(), name.strip()
        if "-" in key:                      # a range key (e.g. 0x02-0x0F) — skip
            continue
        k = _parse_int(key)
        if k is None and re.fullmatch(r"b[01]+", key):   # 'b1001' binary token
            k = int(key[1:], 2)
        if k is not None and name:
            out[k] = name
    return out


@dataclass
class FieldSpec:
    name: str
    hi: int
    lo: int
    reset: Optional[int]
    access: str = ""
    excess1: bool = False
    description: str = ""
    enum: Dict[int, str] = field(default_factory=dict)

    @property
    def mask(self) -> int:
        return ((1 << (self.hi - self.lo + 1)) - 1) << self.lo

    def extract(self, byte_value: int) -> int:
        return (byte_value & self.mask) >> self.lo

    def value_label(self, value: int) -> str:
        """Symbolic name for `value` if the field defines an enum (`valid`
        "v=Name,…"), else the plain number. (Excess-1 fields still report the
        stored value, matching how they appear elsewhere.)"""
        return self.enum.get(value, f"{value}")


@dataclass
class RegisterSpec:
    name: str
    offset: int                 # offset within the block (the _NEXT offset)
    abs_address: int
    reset: Optional[int]
    access: str
    dual_ranked: bool
    curr_offset: Optional[int]
    fields: List[FieldSpec]
    # Addresses this ONE spec covers, for a table row that collapses a run of identical
    # bytes (Reserved / ImpDef filler). Defined register ARRAYS are expanded to one spec
    # per address instead, so their per-index field names survive — see _expand_array.
    span: int = 1

    def reset_byte(self) -> int:
        """Best-effort reset byte: register reset if numeric, else assembled
        from numeric field resets (non-numeric/ImpDef fields contribute 0)."""
        if self.reset is not None:
            return self.reset & 0xFF
        v = 0
        for f in self.fields:
            if f.reset is not None:
                v |= (f.reset << f.lo) & f.mask
        return v & 0xFF

    def decode(self, byte_value: int) -> List[Tuple[str, int, FieldSpec]]:
        return [(f.name, f.extract(byte_value), f) for f in self.fields]


def _expand_array(base: RegisterSpec, array: Optional[dict]) -> List[RegisterSpec]:
    """Turn one table row into the RegisterSpecs it really describes.

    A row covering several addresses is one of two things, and the ``array`` descriptor in
    data/registers.json says which:

    ``{"kind": "bits", "member": N, ...}``
        A bit-per-member group spread over the range, 8 members to a byte, ascending with
        the lowest-numbered member at the LOWEST address and at bit 0 — ``IntStat_SDCA``
        (0x40 = SDCA07:00 … 0x47 = SDCA63:56), ``IntStat_ImpDef``, ``IntCascade_DP``.
        Expands to one spec per address carrying 8 correctly-numbered single-bit fields.
    ``{"kind": "register", "member": N, ...}``
        One whole-byte register per member — ``DPn_EC_TestFailCh`` (0x20 = channel 0's
        8-bit saturating counter … 0x2F = channel 15's). Expands to one spec per address.
    absent
        Filler (Reserved / ImpDef): identical bytes with nothing to index. Kept as the
        SINGLE spec the table shows, spanning the range, so the register view lists one
        row rather than fifty that all say "Reserved" — while every address in the span
        still resolves, so a write into it is identifiable instead of unknown.

    Bit ordering here is the spec's own table layout (Table 166 / Table 169), not an
    inference. One correction: Table 169 prints ``IntCascade_DP11`` at 0x2A bit 1, a
    duplicate of the label at 0x29 bit 3; every other bit of both bytes ascends
    unbroken, so the generated name is DP17.
    """
    if not array or base.span <= 1:
        return [base]
    kind = array.get("kind")
    member = array.get("member") or base.name
    first = int(array.get("first", 0))
    digits = int(array.get("digits", 0))
    src = base.fields[0] if base.fields else None
    out: List[RegisterSpec] = []

    def index(n: int) -> str:
        return f"{n:0{digits}d}" if digits else str(n)

    for i in range(base.span):
        if kind == "bits":
            lo_n = first + i * 8
            fields = [FieldSpec(
                name=f"{member}{index(lo_n + b)}", hi=b, lo=b,
                reset=(src.reset if src else 0), access=base.access,
                description=(src.description if src else ""),
            ) for b in range(8)]
            name = f"{member}[{index(lo_n + 7)}:{index(lo_n)}]"
        elif kind == "register":
            name = f"{member}{index(first + i)}"
            fields = [FieldSpec(
                name=name, hi=7, lo=0, reset=(src.reset if src else 0),
                access=base.access, description=(src.description if src else ""),
            )]
        else:
            return [base]                     # unknown kind: leave the row as declared
        out.append(RegisterSpec(
            name=name, offset=base.offset + i, abs_address=base.abs_address + i,
            reset=base.reset, access=base.access, dual_ranked=base.dual_ranked,
            curr_offset=(base.curr_offset + i) if base.curr_offset is not None else None,
            fields=fields, span=1,
        ))
    return out


@dataclass
class ResolveResult:
    block: str
    register: RegisterSpec
    dp_index: Optional[int]      # data-port index for DP block, else None
    rank: str                    # 'NEXT' | 'CURR' | 'single'


@dataclass
class RegisterMap:
    blocks: Dict[str, List[RegisterSpec]] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)
    # offset (within block) -> RegisterSpec, per block, for _NEXT and _CURR.
    _by_next: Dict[str, Dict[int, RegisterSpec]] = field(default_factory=dict)
    _by_curr: Dict[str, Dict[int, RegisterSpec]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str = DEFAULT_REGISTERS_JSON) -> "RegisterMap":
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        rm = cls(meta=doc.get("_meta", {}))
        for blk in doc.get("blocks", []):
            name = blk["name"]
            regs: List[RegisterSpec] = []
            nxt: Dict[int, RegisterSpec] = {}
            cur: Dict[int, RegisterSpec] = {}
            for r in blk["registers"]:
                offset, span = _parse_offset_span(r.get("offset"))
                if offset is None:
                    continue
                fields = []
                for fdef in r.get("fields", []):
                    try:
                        hi, lo = _parse_bits(fdef["bits"])
                    except (ValueError, KeyError):
                        continue
                    fields.append(FieldSpec(
                        name=fdef.get("name", "?"), hi=hi, lo=lo,
                        reset=_parse_int(fdef.get("reset")),
                        access=fdef.get("access", ""),
                        excess1=bool(fdef.get("excess1", False)),
                        description=fdef.get("description", ""),
                        enum=_parse_enum(fdef.get("valid")),
                    ))
                curr_off = _parse_int(r.get("curr_offset"))
                base = RegisterSpec(
                    name=r.get("name", "?"), offset=offset,
                    abs_address=_parse_int(r.get("abs_address")) or 0,
                    reset=_parse_int(r.get("reset")), access=r.get("access", ""),
                    dual_ranked=bool(r.get("dual_ranked", False)),
                    curr_offset=curr_off, fields=fields, span=span,
                )
                for spec in _expand_array(base, r.get("array")):
                    regs.append(spec)
                    for k in range(spec.span):
                        nxt[spec.offset + k] = spec
                    if spec.dual_ranked:
                        cbase = (spec.curr_offset if spec.curr_offset is not None
                                 else spec.offset + CURR_RANK_OFFSET)
                        for k in range(spec.span):
                            cur[cbase + k] = spec
            rm.blocks[name] = regs
            rm._by_next[name] = nxt
            rm._by_curr[name] = cur
        return rm

    def resolve(self, address: int) -> Optional[ResolveResult]:
        """Resolve a 32-bit (within-device) address to its register + rank."""
        if SLC_BASE <= address < SLC_BASE + DP_STRIDE:
            return self._resolve_in_block("SLC", address - SLC_BASE)
        if CDS_BASE <= address < CDS_BASE + DP_STRIDE:
            return self._resolve_in_block("CDS", address - CDS_BASE)
        for name, base in (("PHY1", PHY1_BASE), ("PHY2", PHY2_BASE), ("PHY3", PHY3_BASE)):
            if base <= address < base + DP_STRIDE:
                return self._resolve_in_block(name, address - base)
        if DP_BASE <= address < DP_BASE + DP_STRIDE * 32:
            dp = (address - DP_BASE) // DP_STRIDE
            off = (address - DP_BASE) % DP_STRIDE
            res = self._resolve_in_block("DP", off)
            if res:
                res.dp_index = dp
            return res
        return None

    def _resolve_in_block(self, block: str, offset: int) -> Optional[ResolveResult]:
        nxt = self._by_next.get(block, {})
        cur = self._by_curr.get(block, {})
        if offset in nxt:
            spec = nxt[offset]
            return ResolveResult(block, spec, None,
                                 "NEXT" if spec.dual_ranked else "single")
        if offset in cur:
            return ResolveResult(block, cur[offset], None, "CURR")
        return None

    def field_summary(self, address: int, byte_value: int) -> str:
        """'NumColumns=3' style summary for a written byte (for command tables)."""
        res = self.resolve(address)
        if not res:
            return ""
        parts = [f"{n}={f.value_label(v)}" for n, v, f in res.register.decode(byte_value)
                 if not n.lower().startswith("reserved")]
        label = res.register.name
        if res.dp_index is not None:
            label = f"DP{res.dp_index}.{label}"
        return f"{label}: " + ", ".join(parts) if parts else label


@dataclass
class _Cell:
    value: int
    provenance: Provenance


class DeviceRegisterFile:
    """Per-device register state with provenance, for the register-map view.

    Reset values are LAZY: an address that was never written resolves its reset
    byte from the spec on read (provenance DEFAULT). Only explicitly set
    addresses are stored, so this scales across 32 data ports x 12 devices.

    Dual-ranked registers are modelled with the hardware's two ranks: a WriteA32
    updates the **_NEXT** value (staged); a confirmed Commit whose device mask
    includes this device and whose commit-group mask intersects the register's
    group copies _NEXT -> **_CURR** (live). The view shows both. Single-ranked
    registers apply immediately (stored as _NEXT, with no separate _CURR).
    """

    def __init__(self, rmap: RegisterMap, device: int, pmap=None):
        self.rmap = rmap
        self.device = device
        # Optional per-device peripheral (vendor) register map for addresses in
        # device-defined space — duck-typed: any object with resolve(addr) ->
        # spec-with-reset_byte(). Lets unwritten vendor registers show their reset.
        self.pmap = pmap
        self._cells: Dict[int, _Cell] = {}      # NEXT/staged (or single-ranked) by addr
        self._curr: Dict[int, _Cell] = {}       # committed _CURR by NEXT-base addr

    def seed_reset(self) -> None:
        """No-op: reset values are served lazily from the spec (see value())."""

    def _reset_byte(self, address: int) -> int:
        res = self.rmap.resolve(address)
        if res:
            return res.register.reset_byte()
        if self.pmap is not None:
            preg = self.pmap.resolve(address)
            if preg is not None:
                return preg.reset_byte()
        return 0

    def _set(self, address: int, value: int, prov: Provenance) -> None:
        self._cells[address] = _Cell(value & 0xFF, prov)

    def apply_write(self, address: int, data: bytes, prov: Provenance = Provenance.WRITTEN) -> None:
        """Apply a WriteA32: consecutive bytes from `address`. Dual-ranked writes
        stage at _NEXT only — they reach _CURR via commit()."""
        for i, b in enumerate(data):
            self._set(address + i, int(b), prov)

    def apply_read(self, address: int, data: bytes) -> None:
        """Apply a Read's returned bytes: the device reported its live value for
        `address`+, recorded with READ provenance (revealed via a read, distinct
        from a WRITTEN bus write). Rank-aware: a read of a dual-ranked register's
        _NEXT alias reveals the staged value and updates _NEXT; a read of its _CURR
        alias reveals the committed value and updates _CURR; single-ranked reads
        update the one value. (A _NEXT read does NOT touch _CURR.)"""
        for i, b in enumerate(data):
            addr = address + i
            b = int(b) & 0xFF
            res = self.rmap.resolve(addr)
            if res is not None and res.register.dual_ranked and res.rank == "CURR":
                spec = res.register
                curr_delta = (spec.curr_offset - spec.offset
                              if spec.curr_offset is not None else CURR_RANK_OFFSET)
                self._curr[addr - curr_delta] = _Cell(b, Provenance.READ)
            else:
                self._set(addr, b, Provenance.READ)

    def _dp_commit_group(self, dp_index: int) -> int:
        """The data port's CommitGroupMemb mask (single-ranked, reset 0b0001=CG0)."""
        addr = DP_BASE + dp_index * DP_STRIDE + DP_COMMIT_GROUP_OFFSET
        c = self._cells.get(addr)
        if c is not None:
            return c.value & 0xFF
        return self._reset_byte(addr) or 0x01

    def commit(self, group_mask: int) -> None:
        """Promote staged _NEXT values to _CURR for dual-ranked registers whose
        commit group intersects `group_mask`. SLC/CDS/PHY dual-ranked registers
        are Commit Group 0; each data port commits iff its CommitGroupMemb mask
        intersects the group mask (Section 9.1.10.4)."""
        for addr, cell in list(self._cells.items()):
            res = self.rmap.resolve(addr)
            if res is None or not res.register.dual_ranked or res.rank != "NEXT":
                continue
            if res.dp_index is not None:
                memb = self._dp_commit_group(res.dp_index)
            else:
                memb = 0x01                       # SLC/CDS/PHY dual-ranked = CG0
            if memb & group_mask:
                self._curr[addr] = _Cell(cell.value, cell.provenance)

    def force_ui(self, address: int, value: int) -> None:
        """Debug what-if: force a register byte to `value` with UI provenance, in
        BOTH ranks — so a dual-ranked register's committed (_CURR) geometry, which
        the bus grid renders, reflects the override too (not just the staged _NEXT).
        Single-ranked registers keep only _cells; the extra _curr entry is unused."""
        cell = _Cell(int(value) & 0xFF, Provenance.UI)
        self._cells[address] = cell
        self._curr[address] = cell

    def seed(self, address: int, cur: int, has_cur: bool, cur_prov: Provenance,
             next_val: int, has_next: bool, next_prov: Provenance) -> None:
        """Seed this file from the C++ decode authority's register snapshot for one
        address (see swi3score.registers_from_commands / CRegisterModel::Snapshot).
        The NEXT display value is the staged value if present, else the committed
        value (which persists in the NEXT column after a commit, matching the wire
        replay). The _CURR bank is set only for dual-ranked registers that have a
        committed value. Each rank carries its own provenance from the authority
        (WRITTEN vs READ vs — for a what-if — UI), so a read-revealed value shows as
        a Bus Read in the map. Used instead of the Python write/commit/read replay so
        register values AND provenance come from the same model as the grid and audio."""
        res = self.rmap.resolve(address)
        dual = bool(res and res.register.dual_ranked)
        if has_next:
            self._cells[address] = _Cell(int(next_val) & 0xFF, next_prov)
        elif has_cur:
            self._cells[address] = _Cell(int(cur) & 0xFF, cur_prov)
        if dual and has_cur:
            self._curr[address] = _Cell(int(cur) & 0xFF, cur_prov)

    def get(self, address: int) -> _Cell:
        c = self._cells.get(address)
        return c if c else _Cell(self._reset_byte(address), Provenance.DEFAULT)
    def value(self, address: int, default: int = 0) -> int:
        c = self._cells.get(address)
        return c.value if c else self._reset_byte(address)

    def provenance(self, address: int) -> Provenance:
        c = self._cells.get(address)
        return c.provenance if c else Provenance.DEFAULT

    def curr_value(self, next_address: int) -> int:
        """Committed (_CURR) value for a dual-ranked register, by its _NEXT-base
        address. Falls back to the reset byte if nothing has been committed yet."""
        c = self._curr.get(next_address)
        return c.value if c else self._reset_byte(next_address)

    def curr_provenance(self, next_address: int) -> Provenance:
        """Provenance of the committed (_CURR) value: DEFAULT until a commit has
        promoted a staged write into it."""
        c = self._curr.get(next_address)
        return c.provenance if c else Provenance.DEFAULT

    def items(self):
        """(address, value) for every explicitly-set register (written / UI / CSV)."""
        return [(addr, cell.value) for addr, cell in self._cells.items()]



def apply_commands(rmap: RegisterMap, commands, up_to_sample: Optional[int] = None,
                   peripheral_maps: Optional[Dict[int, "object"]] = None) -> Dict[int, DeviceRegisterFile]:
    """Build per-device register files by replaying CRC-valid WriteA32 and Commit
    commands in order. A WriteA32 stages dual-ranked values at _NEXT (single-ranked
    apply immediately); a CONFIRMED Commit promotes _NEXT -> _CURR for the targeted
    devices and commit groups (so the view shows what the bus has actually committed
    vs what is merely staged). A CRC-valid Read's returned data also populates the
    addressed register(s) — a read reveals the live device value, so it fills in the
    map just like a write snooped from the bus. The target of a write/commit/read is
    each set bit of its device mask. If `up_to_sample` is given, only commands started
    at or before it are applied — the register state as-of a time cursor.

    `peripheral_maps` optionally maps a device number to its vendor register map, so
    addresses in device-defined space resolve their reset/fields per device.

    NOTE: this pure-Python replay is a TEST ORACLE / offline helper. The live
    as-of-cursor register view uses the C++ authority instead
    (Session.register_files_at -> registers_from_commands), which defers a confirmed
    sync-point commit to its SSP (Row_Delay rows later) rather than its start_sample.
    So the `up_to_sample` filter here is deliberately simpler than the view's timing.
    """
    files: Dict[int, DeviceRegisterFile] = {}
    pmaps = peripheral_maps or {}

    def file_for(dev: int) -> DeviceRegisterFile:
        return files.setdefault(dev, DeviceRegisterFile(rmap, dev, pmaps.get(dev)))

    for c in commands:
        if up_to_sample is not None and c.get("start_sample", 0) > up_to_sample:
            continue
        mask = c.get("device_mask", 0)
        if c.get("command") == "WriteA32" and c.get("crc_valid") and c.get("has_address"):
            for dev in range(12):
                if mask & (1 << dev):
                    file_for(dev).apply_write(c["address"], c["data"])
        elif c.get("is_commit") and c.get("commit_confirmed"):
            group_mask = int(c.get("group_mask", 0))
            # A device_mask==0 commit is bus-wide: apply to every device we track
            # (mirrors C++ CRegisterModel::Commit, which iterates only touched
            # devices). When nothing is tracked yet, targets is [] and this is
            # correctly a no-op -- commit() only promotes a device's staged _NEXT
            # cells, and an untouched device has none, so materialising files for the
            # full 0..11 range would change nothing.
            targets = [d for d in range(12) if mask & (1 << d)] or list(files)
            for dev in targets:
                file_for(dev).commit(group_mask)
        elif (c.get("has_read_data") and c.get("read_data_crc_valid")
              and c.get("has_address")):
            # Read-back: the device returned its live value for this address range.
            read_data = c.get("read_data") or b""
            if read_data:
                for dev in range(12):
                    if mask & (1 << dev):
                        file_for(dev).apply_read(c["address"], read_data)
    return files
