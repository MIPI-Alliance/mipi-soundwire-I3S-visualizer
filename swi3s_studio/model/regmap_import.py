"""Peripheral (chip-specific) register-map import.

SWI3S Studio decodes the SWI3S spec registers from `data/registers.json`. Each
peripheral on the bus can also be a different chip with its own vendor register
map in the device-defined address space (0x10000000-0x3FFFFFFF local). This
module imports such vendor maps into the *same* RegisterSpec/FieldSpec model the
register view renders.

It is deliberately an IMPORT layer, not a new canonical format:

* `RegisterMapImporter` is the interface. Importers are named for the SHAPE of the XML
  they read, never for a vendor.
* `BitListXmlImporter` reads rootless `<register>` elements holding one `<bit>` per bit.
  Per-bit `name[i]` runs are coalesced back into ranged
  fields (`gain[5]..[0]` -> one `gain[5:0]` FieldSpec).
* `RegisterFieldXmlImporter` reads a `<Registers>` list of wide (e.g. 32-bit) registers
  and a separate `<Fields>` list whose entries name their register and bits,
  `REG[15:8]`. Each wide register is split into the byte registers the bus addresses,
  little-endian, and a field that spans bytes is sliced into one piece per byte.
* Import is tolerant: it wraps rootless vendor XML in a synthetic root, strips a BOM /
  XML declaration, and escapes stray ampersands.
* Every import returns an `ImportReport` describing what was inferred and what
  looked off (address-space class, duplicates, out-of-range bits, missing
  resets, dropped vendor attributes) so the assistant can show it before commit.

The normalized result (`PeripheralRegisterMap`) round-trips to a small JSON so a
converted map can be cached in the workspace / `data/regmaps/` and re-opened
without the vendor file.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .. import import_notes
from .registers import FieldSpec, RegisterSpec, _parse_int

# Normalized-JSON schema tag (bump if the on-disk shape changes).
REGMAP_SCHEMA = "swi3s-studio-regmap/1"

# Local device-defined peripheral space (the spec's "Peripheral Top-Level Address Map"
# table). Remote mirror too.
_PERIPHERAL_REGIONS = ((0x10000000, 0x3FFFFFFF), (0x90000000, 0xBFFFFFFF))


def in_peripheral_space(address: int) -> bool:
    """True if `address` is in the device-defined (vendor) register space."""
    a = int(address) & 0xFFFFFFFF
    return any(lo <= a <= hi for lo, hi in _PERIPHERAL_REGIONS)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

@dataclass
class ImportReport:
    """What the importer inferred + anything that looked off. Shown by the
    import assistant before the user commits the map to a device."""
    source_name: str = ""
    format: str = ""                       # importer that handled the file
    register_count: int = 0
    field_count: int = 0                   # after coalescing
    raw_bit_count: int = 0                 # before coalescing
    raw_unit: str = "bits"                 # what raw_bit_count counts, for the summary
    address_min: Optional[int] = None
    address_max: Optional[int] = None
    warnings: List[str] = field(default_factory=list)
    dropped_attributes: List[str] = field(default_factory=list)  # e.g. ['locked', 'group']

    def add(self, msg: str) -> None:
        self.warnings.append(msg)

    def report_notes(self, source: str = "") -> None:
        """Raise each warning through import_notes under `source` (default: the file the
        report is for)."""
        for w in self.warnings:
            import_notes.warn(source or self.source_name, w)

    @property
    def ok(self) -> bool:
        return self.register_count > 0


# ---------------------------------------------------------------------------
# Normalized runtime map
# ---------------------------------------------------------------------------

@dataclass
class PeripheralRegisterMap:
    """A vendor register map normalized into RegisterSpec form, addressed by
    absolute 32-bit address (byte-wide registers, like the on-wire model)."""
    name: str = ""
    registers: List[RegisterSpec] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    _by_addr: Dict[int, RegisterSpec] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self._by_addr:
            self._by_addr = {r.abs_address: r for r in self.registers}

    def resolve(self, address: int) -> Optional[RegisterSpec]:
        return self._by_addr.get(int(address) & 0xFFFFFFFF)

    # ---- normalized JSON (cache / data/regmaps) ----
    def to_json_dict(self) -> dict:
        return {
            "_meta": {"schema": REGMAP_SCHEMA, "name": self.name, **self.meta},
            "registers": [
                {
                    "name": r.name,
                    "address": f"0x{r.abs_address:08X}",
                    "reset": None if r.reset is None else f"0x{r.reset:02X}",
                    "access": r.access,
                    "fields": [_field_json(fs) for fs in r.fields],
                }
                for r in self.registers
            ],
        }

    @classmethod
    def from_json_dict(cls, doc: dict, source: str = "") -> "PeripheralRegisterMap":
        """Read a normalized map. What it cannot read (a register with no address, a field
        with no bits spec, an unparseable reset or value label, a duplicate address) is
        reported through import_notes under `source` (default: the map's name), each
        condition once with its count."""
        meta = dict(doc.get("_meta", {}))
        name = meta.pop("name", "") if isinstance(meta, dict) else ""
        meta.pop("schema", None)
        src = source or str(name or "register map")
        lead = f"register map {name!r}: " if name and source and source != name else ""
        regs: List[RegisterSpec] = []
        seen_addr: Dict[int, str] = {}
        no_addr: List[str] = []
        dupes: List[str] = []
        bad_bits: List[str] = []
        bad_reset: List[str] = []
        bad_enum = 0
        for r in doc.get("registers", []):
            addr = _parse_int(r.get("address"))
            if addr is None:                        # unparseable/missing address:
                no_addr.append(str(r.get("name", "?")))   # skipped, not collapsed to 0
                continue
            if addr in seen_addr:
                # resolve()/_by_addr keeps only the last register at an address.
                dupes.append(f"0x{addr:08X} ({seen_addr[addr]!r} and {r.get('name', '?')!r})")
            seen_addr[addr] = r.get("name", "?")
            if r.get("reset") is not None and _parse_int(r.get("reset")) is None:
                bad_reset.append(str(r.get("name", "?")))
            fields: List[FieldSpec] = []
            for fd in r.get("fields", []):
                bits = str(fd.get("bits", ""))      # tolerate a non-string in cached JSON
                m = re.match(r"\[(\d+)(?::(\d+))?\]", bits.strip())
                if not m:
                    bad_bits.append(f"{r.get('name', '?')}.{fd.get('name', '?')}")
                    continue
                hi = int(m.group(1))
                lo = int(m.group(2)) if m.group(2) is not None else hi
                enum = {}
                for k, v in (fd.get("enum") or {}).items():
                    key = _parse_int(k)
                    if key is not None:
                        enum[key] = str(v)
                    else:
                        bad_enum += 1
                if fd.get("reset") is not None and _parse_int(fd.get("reset")) is None:
                    bad_reset.append(f"{r.get('name', '?')}.{fd.get('name', '?')}")
                fields.append(FieldSpec(
                    name=fd.get("name", "?"), hi=hi, lo=lo,
                    reset=_parse_int(fd.get("reset")), access=fd.get("access", ""),
                    description=str(fd.get("description", "")), enum=enum))
            regs.append(RegisterSpec(
                name=r.get("name", "?"), offset=addr, abs_address=addr,
                reset=_parse_int(r.get("reset")), access=r.get("access", ""),
                dual_ranked=False, curr_offset=None, fields=fields))
        if no_addr:
            import_notes.warn(src, f"{lead}{_count(no_addr, 'register')} with a missing or "
                                   f"unparseable address skipped ({_first(no_addr)})")
        if dupes:
            import_notes.warn(src, f"{lead}{_count(dupes, 'duplicate address', 'es')}: the "
                                   f"later register is used ({_first(dupes)})")
        if bad_bits:
            import_notes.warn(src, f"{lead}{_count(bad_bits, 'field')} with an unparseable "
                                   f"bits spec dropped ({_first(bad_bits)})")
        if bad_reset:
            import_notes.warn(src, f"{lead}{_count(bad_reset, 'unparseable reset')} read as "
                                   f"unknown ({_first(bad_reset)})")
        if bad_enum:
            import_notes.warn(src, f"{lead}{bad_enum} value label(s) with an unparseable "
                                   f"value dropped")
        return cls(name=name, registers=regs, meta=meta)

    def to_json(self) -> str:
        return json.dumps(self.to_json_dict(), indent=2)

    @classmethod
    def from_json(cls, text: str, source: str = "") -> "PeripheralRegisterMap":
        return cls.from_json_dict(json.loads(text), source)


def _count(items: List[str], noun: str, plural: str = "s") -> str:
    return f"{len(items)} {noun}{plural if len(items) != 1 else ''}"


def _first(items: List[str], n: int = 3) -> str:
    more = f", and {len(items) - n} more" if len(items) > n else ""
    return ", ".join(items[:n]) + more


def _field_json(fs: FieldSpec) -> dict:
    """A field as normalized JSON; description and value labels only when it has them, so
    a map without them reads as it always did."""
    out: Dict[str, object] = {"name": fs.name,
           "bits": f"[{fs.hi}:{fs.lo}]" if fs.hi != fs.lo else f"[{fs.hi}]",
           "reset": None if fs.reset is None else int(fs.reset),
           "access": fs.access}
    if fs.description:
        out["description"] = fs.description
    if fs.enum:
        out["enum"] = {str(k): v for k, v in sorted(fs.enum.items())}
    return out


# ---------------------------------------------------------------------------
# Importers
# ---------------------------------------------------------------------------

def _xml_tolerant_parse(text: str) -> ET.Element:
    """Parse vendor XML that may be rootless / BOM-prefixed / have stray '&'.

    Wraps the content in a synthetic root so a flat list of <register> siblings
    parses, after removing a leading BOM and any <?xml?> declaration and
    escaping bare ampersands."""
    text = text.lstrip("﻿")
    text = re.sub(r"^\s*<\?xml[^>]*\?>", "", text, count=1).strip()
    # Escape ampersands that don't start a valid entity (&amp; &#123; &name;).
    text = re.sub(r"&(?!#?\w+;)", "&amp;", text)
    return ET.fromstring(f"<__regmap_root__>{text}</__regmap_root__>")


class RegisterMapImporter:
    """Interface for a vendor-format register-map importer."""
    format_name = "generic"

    @staticmethod
    def sniff(text: str) -> bool:                       # pragma: no cover - overridden
        return False

    def parse(self, text: str, source_name: str = "",
              coalesce: bool = True, default_access: str = "") \
            -> Tuple[PeripheralRegisterMap, ImportReport]:   # pragma: no cover
        raise NotImplementedError


_BIT_INDEX_RE = re.compile(r"^(?P<base>.+?)\[(?P<idx>\d+)\]$")


class BitListXmlImporter(RegisterMapImporter):
    """Rootless `<register name addr><bit position name access/>...<reset hex/></register>`."""
    format_name = "bit-list-xml"

    @staticmethod
    def sniff(text: str) -> bool:
        head = text[:4000]
        return "<register" in head and "<bit " in head and "position=" in head

    def parse(self, text: str, source_name: str = "",
              coalesce: bool = True, default_access: str = "") \
            -> Tuple[PeripheralRegisterMap, ImportReport]:
        report = ImportReport(source_name=source_name, format=self.format_name)
        root = _xml_tolerant_parse(text)

        name = source_name or "peripheral"
        registers: List[RegisterSpec] = []
        seen_addr: Dict[int, str] = {}
        dropped: set = set()
        dropped_reg: set = set()
        bad_reset: List[str] = []
        in_spec_space = 0

        for reg_el in root.iter("register"):
            reg_name = reg_el.get("name", "?")
            addr = _parse_int(reg_el.get("address"))
            if addr is None:
                report.add(f"register {reg_name!r}: missing/invalid address — skipped")
                continue
            if addr in seen_addr:
                report.add(f"duplicate address 0x{addr:08X} ({reg_name!r} and {seen_addr[addr]!r})")
            seen_addr[addr] = reg_name
            if not in_peripheral_space(addr):
                in_spec_space += 1

            reset_el = reg_el.find("reset")
            reg_reset = _parse_int(reset_el.get("hex")) if reset_el is not None else None
            if reset_el is not None and reg_reset is None:
                bad_reset.append(reg_name)
            dropped_reg.update(a for a in reg_el.attrib if a not in ("name", "address", "access"))
            dropped_reg.update(f"<{c.tag}>" for c in reg_el if c.tag not in ("bit", "reset"))

            # Collect bits (position, name, access); note dropped vendor attrs.
            bits: List[Tuple[int, str, str]] = []
            for bit_el in reg_el.findall("bit"):
                pos = _parse_int(bit_el.get("position"))
                if pos is None or not (0 <= pos <= 7):
                    report.add(f"{reg_name!r}: bit position {bit_el.get('position')!r} out of range 0-7 — skipped")
                    continue
                if any(b[0] == pos for b in bits):
                    report.add(f"{reg_name!r}: bit position {pos} is listed twice — both kept")
                bits.append((pos, bit_el.get("name", "?"), bit_el.get("access", default_access)))
                for attr in bit_el.attrib:
                    if attr not in ("position", "name", "access"):
                        dropped.add(attr)
            report.raw_bit_count += len(bits)

            fields = self._make_fields(bits, reg_reset, coalesce, reg_name, report)
            registers.append(RegisterSpec(
                name=reg_name, offset=addr, abs_address=addr,
                reset=reg_reset, access=reg_el.get("access", default_access),
                dual_ranked=False, curr_offset=None, fields=fields))
            if reg_reset is None and not fields:
                report.add(f"{reg_name!r}: no reset and no fields")

        registers.sort(key=lambda r: r.abs_address)
        report.register_count = len(registers)
        report.field_count = sum(len(r.fields) for r in registers)
        report.dropped_attributes = sorted(dropped)
        if registers:
            report.address_min = registers[0].abs_address
            report.address_max = registers[-1].abs_address
        if in_spec_space:
            report.add(f"{in_spec_space} register(s) fall outside device-defined space "
                       f"(0x10000000-0x3FFFFFFF) and may shadow SWI3S spec registers")
        if dropped:
            report.add(f"dropped vendor bit attributes (not modelled): {', '.join(sorted(dropped))}")
        if dropped_reg:
            report.add(f"dropped register attributes and elements (not modelled): "
                       f"{', '.join(sorted(dropped_reg))}")
        if bad_reset:
            report.add(f"{len(bad_reset)} register(s) with an unparseable <reset hex> read as "
                       f"no reset: {', '.join(bad_reset[:3])}"
                       f"{f', and {len(bad_reset) - 3} more' if len(bad_reset) > 3 else ''}")

        return PeripheralRegisterMap(
            name=name, registers=registers,
            meta={"source_format": self.format_name, "source_name": source_name}), report

    @staticmethod
    def _make_fields(bits: List[Tuple[int, str, str]], reg_reset: Optional[int],
                     coalesce: bool, reg_name: str, report: ImportReport) -> List[FieldSpec]:
        """Coalesce `base[i]` per-bit runs into ranged FieldSpecs. Singletons and
        non-contiguous index runs stay split. Field resets are sliced from the
        register reset byte."""
        def field_reset(hi: int, lo: int) -> Optional[int]:
            if reg_reset is None:
                return None
            mask = ((1 << (hi - lo + 1)) - 1) << lo
            return (reg_reset & mask) >> lo

        if not coalesce:
            return [FieldSpec(name=nm, hi=pos, lo=pos, reset=field_reset(pos, pos), access=acc)
                    for pos, nm, acc in sorted(bits, key=lambda b: -b[0])]

        # Group indexed bits by base name; keep singletons as-is.
        groups: Dict[str, List[Tuple[int, int, str]]] = {}   # base -> [(position, index, access)]
        singles: List[Tuple[int, str, str]] = []
        for pos, nm, acc in bits:
            m = _BIT_INDEX_RE.match(nm)
            if m:
                groups.setdefault(m.group("base"), []).append((pos, int(m.group("idx")), acc))
            else:
                singles.append((pos, nm, acc))

        out: List[FieldSpec] = []
        for base, members in groups.items():
            # Split into runs of contiguous bit positions (ordered high->low).
            members.sort(key=lambda m: -m[0])
            run: List[Tuple[int, int, str]] = []
            for m in members:
                if run and m[0] == run[-1][0] - 1:
                    run.append(m)
                else:
                    if run:
                        out.append(_field_from_run(base, run, field_reset, report, reg_name))
                    run = [m]
            if run:
                out.append(_field_from_run(base, run, field_reset, report, reg_name))
        for pos, nm, acc in singles:
            out.append(FieldSpec(name=nm, hi=pos, lo=pos, reset=field_reset(pos, pos), access=acc))

        out.sort(key=lambda fs: -fs.hi)
        return out


def _field_from_run(base: str, run: List[Tuple[int, int, str]], field_reset,
                    report: ImportReport, reg_name: str) -> FieldSpec:
    hi = run[0][0]
    lo = run[-1][0]
    accesses = {acc for _, _, acc in run}
    if len(accesses) > 1:
        report.add(f"{reg_name!r}: field {base!r} has mixed access {sorted(accesses)} — using {run[0][2]!r}")
    return FieldSpec(name=base, hi=hi, lo=lo, reset=field_reset(hi, lo), access=run[0][2])


_BITS_REF_RE = re.compile(r"^\s*(?P<reg>[^\[\]\s]+)\s*\[(?P<hi>\d+)(?::(?P<lo>\d+))?\]\s*$")
_ACCESS = {"R/W": "RW", "R/O": "RO", "W/O": "WO", "RW": "RW", "RO": "RO", "WO": "WO"}
# The children each importer reads; anything else on a register or field is reported as
# not modelled, by its own name, so nothing about a dialect is hard-coded beyond these.
_REG_READ = {"Name", "Address", "DefaultValue", "Access"}
_FIELD_READ = {"Name", "Address", "DefaultValue", "Access", "Description", "Values"}


_FORMATS = {"hex": 4, "h": 4, "bin": 1, "binary": 1, "b": 1, "dec": 0, "decimal": 0, "d": 0}


def _default_digits(text: Optional[str], fmt: str = "") -> Tuple[str, Optional[int]]:
    """(digits, bits per digit: 4 hex, 1 binary, 0 decimal, None unreadable) of a default.
    A `format` attribute (hex, bin, dec) decides; without one, `0x` is hex, `0b` binary, a
    multi-digit run of 0, 1 and X binary, and anything else decimal. Underscores are
    separators."""
    t = (text or "").strip().replace("_", "")
    bits = _FORMATS.get(fmt.strip().lower()) if fmt.strip() else None
    if t[:2].lower() in ("0x", "0b") and bits in (None, 4 if t[1] in "xX" else 1):
        return t[2:], 4 if t[1] in "xX" else 1
    if bits is not None:
        return t, bits
    if fmt.strip():
        return t, None
    if len(t) > 1 and set(t.upper()) <= {"0", "1", "X"}:
        return t, 1
    return t, 0


def _ambiguous_binary(text: Optional[str], fmt: str = "") -> bool:
    """True for a default read as binary only because its digits are 0s and 1s (`10`): no
    prefix, no `X`, no format attribute. Such a default is reported."""
    t = (text or "").strip().replace("_", "")
    return (not fmt.strip() and len(t) > 1 and set(t) <= {"0", "1"})


def _parse_default(text: Optional[str], width: int, fmt: str = "") -> Tuple[int, int]:
    """(value, known-bit mask) of a default written as hex (`0x1F`, digits `X` unknown),
    binary with unknowns (`0b10`, `0000_00X1`), a bare `X` (all unknown) or decimal, as
    `_default_digits` reads it. Bits above the written digits are known zeros, or unknown
    when the leftmost digit is `X` (`0x1` on 8 bits is 0x01; `0xX1` on 16 bits knows only
    bits 3:0). Unparseable or absent: nothing known."""
    full = (1 << width) - 1
    raw = (text or "").strip().replace("_", "")
    if not raw or raw.upper() == "X":
        return 0, 0
    digits, base_bits = _default_digits(text, fmt)
    if base_bits is None or not digits:
        return 0, 0
    if base_bits == 0:
        v = _parse_int(digits) if digits.isdigit() else None
        return (0, 0) if v is None else (v & full, full)
    value = known = 0
    for ch in digits:
        value <<= base_bits
        known <<= base_bits
        if ch.upper() == "X":
            continue
        try:
            value |= int(ch, 16 if base_bits == 4 else 2)
        except ValueError:
            return 0, 0
        known |= (1 << base_bits) - 1
    if digits[0].upper() != "X":
        known |= full & ~((1 << (base_bits * len(digits))) - 1)
    return value & full, known & full


def _note_default(report: "ImportReport", what: str, el) -> None:
    """Report a default whose reading was a guess or failed: an unknown `format`, text that
    is not a number, or 0s and 1s read as binary with no prefix or format."""
    d_el = el.find("DefaultValue")
    if d_el is None:
        return
    text, fmt = (d_el.text or "").strip(), d_el.get("format", "")
    if fmt.strip() and fmt.strip().lower() not in _FORMATS:
        report.add(f"{what}: default {text!r} has format {fmt!r}, which is not hex, bin or "
                   f"dec — read as nothing known")
    elif text and text.upper() != "X" and _parse_default(text, 64, fmt) == (0, 0):
        report.add(f"{what}: default {text!r} is not a number — read as nothing known")
    elif _ambiguous_binary(text, fmt):
        value = int(text.replace("_", ""), 2)
        report.add(f"{what}: default {text!r} read as binary ({value}); write 0b{text} or "
                   f"format=\"dec\" if it is decimal")


def _text(el, tag: str) -> str:
    child = el.find(tag)
    return (child.text or "").strip() if child is not None and child.text else ""


class RegisterFieldXmlImporter(RegisterMapImporter):
    """`<Registers><Register><Name/><Address/><DefaultValue/><Access/></Register>…` with the
    fields listed apart: `<Fields><Field><Name/><Address>REG[hi:lo]</Address>…</Field>`.

    Registers may be wider than a byte (32-bit is usual) at byte addresses, so each one is
    split into the byte registers the bus reads and writes: byte k of a register at A is
    A + k and holds bits 8k+7..8k (little-endian; a word holding the spec's ID registers
    reads ID0 in bits 7:0 and PartID_H in 31:24, exactly as the bus delivers them). A field
    inside one byte keeps its name; a field across bytes becomes one piece per byte, named
    with the field's own bits (`GAIN[15:8]`). A register's width is the widest of its
    default's digits and its fields' top bits, rounded up to 8, 16, 32 or 64 bits.
    Registers outside device-defined space are skipped and counted: the spec register map
    decodes those addresses, and a vendor's copy of a spec register would only be listed
    twice."""
    format_name = "register-field-xml"

    @staticmethod
    def sniff(text: str) -> bool:
        return all(tag in text for tag in ("<Registers", "<Register>", "<Fields", "<Field>",
                                           "<Address>"))

    def parse(self, text: str, source_name: str = "",
              coalesce: bool = True, default_access: str = "") \
            -> Tuple[PeripheralRegisterMap, ImportReport]:
        report = ImportReport(source_name=source_name, format=self.format_name,
                              raw_unit="fields listed")
        root = _xml_tolerant_parse(text)
        dropped: set = set()

        fields_by_reg: Dict[str, List[tuple]] = {}
        swapped: List[str] = []
        bad_values = 0
        for f_el in root.iter("Field"):
            ref = _BITS_REF_RE.match(_text(f_el, "Address"))
            if not ref:
                report.add(f"field {_text(f_el, 'Name')!r}: no REG[hi:lo] address — skipped")
                continue
            hi = int(ref.group("hi"))
            lo = int(ref.group("lo")) if ref.group("lo") is not None else hi
            if lo > hi:
                hi, lo = lo, hi
                swapped.append(_text(f_el, "Name") or ref.group("reg"))
            enum: Dict[int, str] = {}
            values = f_el.find("Values")
            for v_el in (values if values is not None else []):
                key = _parse_int(v_el.get("Value"))
                label = _text(v_el, "Description")
                if key is not None and label:
                    enum[key] = label
                else:
                    bad_values += 1
            f_dv = f_el.find("DefaultValue")
            f_fmt = f_dv.get("format", "") if f_dv is not None else ""
            dv, dk = _parse_default(_text(f_el, "DefaultValue") or None, hi - lo + 1, f_fmt)
            _note_default(report, f"field {_text(f_el, 'Name') or ref.group('reg')!r}", f_el)
            fields_by_reg.setdefault(ref.group("reg"), []).append((
                hi, lo, _text(f_el, "Name") or ref.group("reg"),
                dv if dk == (1 << (hi - lo + 1)) - 1 else None,
                f_el.find("DefaultValue") is not None,
                _ACCESS.get(_text(f_el, "Access"), _text(f_el, "Access") or default_access),
                _text(f_el, "Description"), enum))
            dropped.update(c.tag for c in f_el if c.tag not in _FIELD_READ)
            report.raw_bit_count += 1

        registers: List[RegisterSpec] = []
        seen_addr: Dict[int, str] = {}
        in_spec_space = 0
        unknown_reset = 0
        named = set()
        for r_el in root.iter("Register"):
            name = _text(r_el, "Name")
            addr = _parse_int(_text(r_el, "Address") or None)
            if not name or addr is None:
                if r_el.find("Address") is not None:
                    report.add(f"register {name or '?'!r}: missing/invalid address — skipped")
                continue
            named.add(name)
            dropped.update(c.tag for c in r_el if c.tag not in _REG_READ)
            fields = fields_by_reg.get(name, [])
            raw = _text(r_el, "DefaultValue")
            r_dv = r_el.find("DefaultValue")
            r_fmt = r_dv.get("format", "") if r_dv is not None else ""
            digits, d_bits = _default_digits(raw, r_fmt)
            from_default = d_bits * len(digits) if d_bits else 0
            # The widest of the default's digits and the fields, rounded up to 8, 16, 32
            # or 64 bits: a register is a power-of-two number of bytes.
            width = max([8, from_default] + [hi + 1 for hi, *_ in fields])
            width = min(64, 1 << (width - 1).bit_length())
            if not in_peripheral_space(addr):
                in_spec_space += 1
                continue
            if addr % (width // 8):
                report.add(f"{name!r}: 0x{addr:08X} is not aligned to its {width}-bit width")
            reg_value, reg_known = _parse_default(raw or None, width, r_fmt)
            _note_default(report, f"register {name!r}", r_el)
            if reg_known != (1 << width) - 1:
                unknown_reset += 1
            access = _ACCESS.get(_text(r_el, "Access"), _text(r_el, "Access") or default_access)
            for k in range(width // 8):
                b_lo, b_hi = 8 * k, 8 * k + 7
                b_addr = addr + k
                b_name = name if width == 8 else f"{name}[{b_hi}:{b_lo}]"
                if b_addr in seen_addr:
                    report.add(f"duplicate address 0x{b_addr:08X} ({b_name!r} and "
                               f"{seen_addr[b_addr]!r})")
                seen_addr[b_addr] = b_name
                b_reset = ((reg_value >> b_lo) & 0xFF
                           if (reg_known >> b_lo) & 0xFF == 0xFF else None)
                b_fields: List[FieldSpec] = []
                for hi, lo, f_name, f_reset, f_has_default, f_access, desc, enum in fields:
                    p_hi, p_lo = min(hi, b_hi), max(lo, b_lo)
                    if p_hi < p_lo:
                        continue
                    whole = (p_hi, p_lo) == (hi, lo)
                    if f_has_default:
                        p_reset = (None if f_reset is None
                                   else (f_reset >> (p_lo - lo)) & ((1 << (p_hi - p_lo + 1)) - 1))
                    else:
                        mask = (1 << (p_hi - p_lo + 1)) - 1
                        p_reset = ((reg_value >> p_lo) & mask
                                   if (reg_known >> p_lo) & mask == mask else None)
                    piece = (f_name if whole else
                             f"{f_name}[{p_hi - lo}:{p_lo - lo}]" if p_hi > p_lo
                             else f"{f_name}[{p_hi - lo}]")
                    b_fields.append(FieldSpec(
                        name=piece, hi=p_hi - b_lo, lo=p_lo - b_lo, reset=p_reset,
                        access=f_access, description=desc, enum=enum if whole else {}))
                b_fields.sort(key=lambda fs: -fs.hi)
                registers.append(RegisterSpec(
                    name=b_name, offset=b_addr, abs_address=b_addr, reset=b_reset,
                    access=access, dual_ranked=False, curr_offset=None, fields=b_fields))
            for hi, _lo, f_name, *_rest in fields:
                if hi >= width:
                    report.add(f"{name!r}: field {f_name!r} reaches bit {hi}, past {width}")

        if swapped:
            report.add(f"{len(swapped)} field(s) written low bit first read as [hi:lo]: "
                       f"{', '.join(swapped[:3])}"
                       f"{f', and {len(swapped) - 3} more' if len(swapped) > 3 else ''}")
        if bad_values:
            report.add(f"{bad_values} value label(s) with no parseable Value or no Description "
                       f"dropped")
        orphans = sorted(set(fields_by_reg) - named)
        if orphans:
            report.add(f"{sum(len(fields_by_reg[o]) for o in orphans)} field(s) name a register "
                       f"the file does not define — skipped")
        registers.sort(key=lambda r: r.abs_address)
        report.register_count = len(registers)
        report.field_count = sum(len(r.fields) for r in registers)
        report.dropped_attributes = sorted(dropped)
        if registers:
            report.address_min = registers[0].abs_address
            report.address_max = registers[-1].abs_address
        if in_spec_space:
            report.add(f"{in_spec_space} register(s) outside device-defined space "
                       f"(0x10000000-0x3FFFFFFF) skipped: the SWI3S spec map decodes those "
                       f"addresses")
        if unknown_reset:
            report.add(f"{unknown_reset} register(s) with an unknown reset in part or whole: "
                       f"those bytes take their fields' resets")
        if dropped:
            report.add(f"not modelled: {', '.join(sorted(dropped))}")
        return PeripheralRegisterMap(
            name=source_name or "peripheral", registers=registers,
            meta={"source_format": self.format_name, "source_name": source_name}), report


# Registry — first importer whose sniff() matches wins.
_IMPORTERS: List[type] = [BitListXmlImporter, RegisterFieldXmlImporter]


def import_text(text: str, source_name: str = "", coalesce: bool = True,
                default_access: str = "") -> Tuple[PeripheralRegisterMap, ImportReport]:
    """Import vendor register-map text, auto-detecting the format."""
    for importer_cls in _IMPORTERS:
        if importer_cls.sniff(text):
            return importer_cls().parse(text, source_name, coalesce, default_access)
    raise ValueError("Unrecognized register-map format (no importer matched)")


def import_file(path: str, coalesce: bool = True, default_access: str = "") \
        -> Tuple[PeripheralRegisterMap, ImportReport]:
    import os
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    return import_text(text, os.path.splitext(os.path.basename(path))[0],
                       coalesce, default_access)


def library_dir() -> str:
    """Path to the bundled register-map library (data/regmaps), across dev,
    installed (pip data-files), and PyInstaller layouts."""
    import os
    import sys
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # swi3s_studio/
    candidates = [
        os.path.join(os.path.dirname(here), "data", "regmaps"),          # repo layout
        os.path.join(here, "data", "regmaps"),                           # packaged in pkg
        os.path.join(sys.prefix, "share", "swi3s-studio", "regmaps"),    # pip data-files
    ]
    base = getattr(sys, "_MEIPASS", None)                                # PyInstaller bundle
    if base:
        candidates.insert(0, os.path.join(base, "data", "regmaps"))
    for c in candidates:
        if os.path.isdir(c):
            return c
    return candidates[0]
