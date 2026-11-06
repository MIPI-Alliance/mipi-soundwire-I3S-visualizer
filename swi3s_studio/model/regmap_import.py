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

import codecs
import json
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .. import import_notes
from .registers import FieldSpec, RegisterSpec, _parse_int, bundled_data_path

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
    _each: Dict[str, List[int]] = field(default_factory=dict, repr=False, compare=False)

    def add(self, msg: str) -> None:
        self.warnings.append(msg)

    def add_each(self, kind: str, msg: str, limit: int = 3) -> None:
        """Add a warning that can repeat once per item: the first `limit` of a `kind` are
        listed and the rest are counted for `settle`, so a file of bad items stays a short
        list."""
        seen = self._each.setdefault(kind, [0, limit])
        seen[0] += 1
        if seen[0] <= limit:
            self.warnings.append(msg)

    def settle(self) -> None:
        """Add one line per `add_each` kind that had more items than were listed."""
        for kind, (n, limit) in self._each.items():
            if n > limit:
                self.warnings.append(f"{n - limit} more {kind} warning(s) not listed")
        self._each.clear()

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
        """Read a normalized map. What it cannot read (a register with no address or one
        outside 32 bits, a field whose bits spec is unparseable, inverted or beyond bit 7,
        an unparseable reset or value label, a duplicate address, a part of the wrong JSON
        type) is reported through import_notes under `source` (default: the map's name),
        each condition once with its count."""
        wrong: List[str] = []
        if not isinstance(doc, dict):
            wrong.append("the document is not a mapping")
            doc = {}
        meta = doc.get("_meta", {})
        if not isinstance(meta, dict):
            wrong.append("_meta is not a mapping")
            meta = {}
        meta = dict(meta)
        name = meta.pop("name", "")
        meta.pop("schema", None)
        src = source or str(name or "register map")
        lead = f"register map {name!r}: " if name and source and source != name else ""
        reg_list = doc.get("registers", [])
        if not isinstance(reg_list, list):
            wrong.append("registers is not a list")
            reg_list = []
        regs: List[RegisterSpec] = []
        seen_addr: Dict[int, str] = {}
        no_addr: List[str] = []
        far_addr: List[str] = []
        dupes: List[str] = []
        bad_bits: List[str] = []
        bad_reset: List[str] = []
        wide_reset: List[str] = []
        bad_enum = 0

        def text(part: dict, key: str, default: str, what: str) -> str:
            """`part[key]` when it is text, `default` when it is absent; any other value
            is a part of the wrong type."""
            value = part.get(key, default)
            if isinstance(value, str):
                return value
            wrong.append(f"{what} that is not text")
            return default

        for r in reg_list:
            if not isinstance(r, dict):
                wrong.append("a register that is not a mapping")
                continue
            rname = text(r, "name", "?", "a register name")
            raccess = text(r, "access", "", "a register access")
            addr = _parse_int(r.get("address"))
            if addr is None:                        # unparseable/missing address:
                no_addr.append(str(rname))   # skipped, not collapsed to 0
                continue
            if not 0 <= addr <= 0xFFFFFFFF:
                far_addr.append(str(rname))
                continue
            if addr in seen_addr:
                # resolve()/_by_addr keeps only the last register at an address.
                dupes.append(f"0x{addr:08X} ({seen_addr[addr]!r} and {rname!r})")
            seen_addr[addr] = rname
            reg_reset = _parse_int(r.get("reset"))
            if r.get("reset") is not None and reg_reset is None:
                bad_reset.append(str(rname))
            elif reg_reset is not None and not 0 <= reg_reset <= 0xFF:
                wide_reset.append(str(rname))
                reg_reset = None
            fields: List[FieldSpec] = []
            field_list = r.get("fields", [])
            if not isinstance(field_list, list):
                wrong.append(f"{rname}.fields is not a list")
                field_list = []
            for fd in field_list:
                if not isinstance(fd, dict):
                    wrong.append("a field that is not a mapping")
                    continue
                fname = text(fd, "name", "?", "a field name")
                faccess = text(fd, "access", "", "a field access")
                bits = str(fd.get("bits", ""))      # tolerate a non-string in cached JSON
                m = re.match(r"\[([0-9]{1,9})(?::([0-9]{1,9}))?\]", bits.strip())
                hi = int(m.group(1)) if m else -1
                lo = int(m.group(2)) if m and m.group(2) is not None else hi
                if not m or hi < lo or hi > 7:      # the byte registers hold bits 7..0
                    bad_bits.append(f"{rname}.{fname}")
                    continue
                enum = {}
                enum_doc = fd.get("enum") or {}
                if not isinstance(enum_doc, dict):
                    wrong.append(f"{rname}.{fname}.enum is not "
                                 f"a mapping")
                    enum_doc = {}
                for k, v in enum_doc.items():
                    key = _parse_int(k)
                    if key is not None and 0 <= key < 1 << (hi - lo + 1):
                        enum[key] = str(v)
                    else:
                        bad_enum += 1
                f_reset = _parse_int(fd.get("reset"))
                if fd.get("reset") is not None and f_reset is None:
                    bad_reset.append(f"{rname}.{fname}")
                elif f_reset is not None and not 0 <= f_reset < 1 << (hi - lo + 1):
                    wide_reset.append(f"{rname}.{fname}")
                    f_reset = None
                fields.append(FieldSpec(
                    name=fname, hi=hi, lo=lo,
                    reset=f_reset, access=faccess,
                    description=str(fd.get("description", "")), enum=enum))
            regs.append(RegisterSpec(
                name=rname, offset=addr, abs_address=addr,
                reset=reg_reset, access=raccess,
                dual_ranked=False, curr_offset=None, fields=fields))
        if wrong:
            import_notes.warn(src, f"{lead}{len(wrong)} part(s) of the wrong type skipped "
                                   f"({_first(sorted(set(wrong)))})")
        if no_addr:
            import_notes.warn(src, f"{lead}{_count(no_addr, 'register')} with a missing or "
                                   f"unparseable address skipped ({_first(no_addr)})")
        if far_addr:
            import_notes.warn(src, f"{lead}{_count(far_addr, 'register')} with an address "
                                   f"outside 0..0xFFFFFFFF skipped ({_first(far_addr)})")
        if dupes:
            import_notes.warn(src, f"{lead}{_count(dupes, 'duplicate address', 'es')}: the "
                                   f"later register is used ({_first(dupes)})")
        if bad_bits:
            import_notes.warn(src, f"{lead}{_count(bad_bits, 'field')} with a bits spec that "
                                   f"is not [hi:lo] within 7..0 dropped ({_first(bad_bits)})")
        if bad_reset:
            import_notes.warn(src, f"{lead}{_count(bad_reset, 'unparseable reset')} read as "
                                   f"unknown ({_first(bad_reset)})")
        if wide_reset:
            import_notes.warn(src, f"{lead}{_count(wide_reset, 'reset')} outside the register's "
                                   f"byte or the field's width read as unknown "
                                   f"({_first(wide_reset)})")
        if bad_enum:
            import_notes.warn(src, f"{lead}{bad_enum} value label(s) with an unparseable "
                                   f"value or one outside the field's width dropped")
        return cls(name=name, registers=regs, meta=meta)

    def to_json(self) -> str:
        return json.dumps(self.to_json_dict(), indent=2)

    @classmethod
    def from_json(cls, text: str, source: str = "") -> "PeripheralRegisterMap":
        return cls.from_json_dict(json.loads(text), source)


def _parse_hex(value: Optional[str]) -> Optional[int]:
    """A hexadecimal token with or without a `0x` prefix (`10` is 0x10), or None."""
    t = (value or "").strip().replace("_", "")
    if t[:2].lower() == "0x":
        t = t[2:]
    try:
        return int(t, 16)
    except ValueError:
        return None


def _count(items: List[str], noun: str, plural: str = "s") -> str:
    return f"{len(items)} {noun}{plural if len(items) != 1 else ''}"


def _clip(text, n: int = 40) -> str:
    """`text` as a string of at most `n` characters, for a warning that echoes file input."""
    t = str(text)
    return t if len(t) <= n else t[:n] + "…"


def _first(items: List[str], n: int = 3) -> str:
    """The first `n` of `items`, each clipped to 120 characters, and a count of the rest."""
    more = f", and {len(items) - n} more" if len(items) > n else ""
    return ", ".join(_clip(i, 120) for i in items[:n]) + more


def _listed(items: List[str], n: int = 8) -> str:
    """The first `n` of `items`, each clipped, and a count of the rest."""
    more = f", and {len(items) - n} more" if len(items) > n else ""
    return ", ".join(_clip(i) for i in items[:n]) + more


def _field_json(fs: FieldSpec) -> dict:
    """A field as normalized JSON; the description and value labels are written only when the
    field has them."""
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

_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>")
_DOCTYPE_OPEN = re.compile(r"<!DOCTYPE", re.IGNORECASE)
_DOCTYPE_STOP = re.compile(r"[\[>]")
_ROOT_OPEN = "<__regmap_root__>"


def _blank_text(text: str) -> str:
    """`text` with every character but newlines made a space, so the lines and columns of
    what follows keep their place."""
    return re.sub(r"[^\n]", " ", text)


def _blank(m: "re.Match") -> str:
    return _blank_text(m.group())


def _doctype_span(text: str) -> Optional[Tuple[int, int]]:
    """(start, end) of the first DOCTYPE declaration, with or without an internal subset
    ([...] may hold '>' inside its declarations), or None when there is none or the first one
    is not terminated. One forward scan, so the cost is linear in the text."""
    m = _DOCTYPE_OPEN.search(text)
    if m is None:
        return None
    pos = m.end()
    while True:
        stop = _DOCTYPE_STOP.search(text, pos)
        if stop is None:
            return None
        if stop.group() == ">":
            return m.start(), stop.end()
        close = text.find("]", stop.end())
        if close < 0:
            return None
        pos = close + 1


def _xml_tolerant_parse(text: str, source: str = "") -> ET.Element:
    """Parse vendor XML that may be rootless / BOM-prefixed / have stray '&'.

    Wraps the content in a synthetic root so a flat list of <register> siblings
    parses, after removing a leading BOM, any <?xml?> declaration and any DOCTYPE
    (entities it declares are not defined) and escaping bare ampersands. Malformed XML
    raises ValueError naming `source` and the line.
    """
    text = text.lstrip("\ufeff")
    text = _XML_DECLARATION.sub(_blank, text, count=1)
    span = _doctype_span(text)
    if span is not None:
        text = text[:span[0]] + _blank_text(text[span[0]:span[1]]) + text[span[1]:]
    # Escape ampersands that don't start a valid entity (&amp; &#123; &name;).
    text = re.sub(r"&(?!#?\w+;)", "&amp;", text)
    try:
        return ET.fromstring(f"{_ROOT_OPEN}{text}</__regmap_root__>")
    except ET.ParseError as exc:
        line = exc.position[0] if getattr(exc, "position", None) else 0
        what = str(exc).split(": line ")[0]
        where = f"{source}: " if source else ""
        raise ValueError(f"{where}not well-formed XML ({what}, line {line})") from None


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


# Access spellings read as written (case aside); anything else is reported.
_ACCESS = {"R/W": "RW", "R/O": "RO", "W/O": "WO", "RW": "RW", "RO": "RO", "WO": "WO",
           "R/W1C": "RW1C", "RW1C": "RW1C"}


def _access_of(text: Optional[str], default_access: str, who: str,
               audit: Dict[str, List[str]]) -> str:
    """The access of a register or field written as `text` (`default_access` when blank): the
    spelling's canonical RW, RO, WO or RW1C. A spelling that is none of those is kept as
    written and recorded in `audit` (spelling -> names) for `_report_access`; a blank access
    is a value not given and is not recorded."""
    t = (text or "").strip() or default_access
    canon = _ACCESS.get(t.upper())
    if canon is not None:
        return canon
    if t:
        audit.setdefault(t, []).append(who)
    return t


def _report_access(report: "ImportReport", audit: Dict[str, List[str]]) -> None:
    """One import warning per access spelling `_access_of` did not recognise, the first few
    spellings listed and the rest counted (`ImportReport.add_each`)."""
    for value, names in audit.items():
        report.add_each("access spelling",
                        f"{len(names)} register(s) or field(s) with access {_clip(value)!r}, "
                        f"which is not RW, RO, WO or RW1C; kept as written: "
                        f"{_listed(names, 3)}")


_BIT_INDEX_RE = re.compile(r"^(?P<base>.+?)\[(?P<idx>[0-9]{1,9})\]$")


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
        root = _xml_tolerant_parse(text, source_name)

        name = source_name or "peripheral"
        registers: List[RegisterSpec] = []
        seen_addr: Dict[int, str] = {}
        dropped: set = set()
        dropped_reg: set = set()
        bad_reset: List[str] = []
        access_audit: Dict[str, List[str]] = {}
        in_spec_space = 0

        for reg_el in root.iter("register"):
            reg_name = reg_el.get("name", "?")
            addr = _parse_int(reg_el.get("address"))
            if addr is None:
                report.add_each("missing address",
                                f"register {_clip(reg_name)!r}: missing/invalid address "
                                f"— skipped")
                continue
            if not 0 <= addr <= 0xFFFFFFFF:
                report.add_each("address outside 32 bits",
                                f"register {_clip(reg_name)!r}: address "
                                f"{_clip(reg_el.get('address'))!r} is outside 0..0xFFFFFFFF "
                                f"— skipped")
                continue
            if addr in seen_addr:
                report.add_each("duplicate address",
                                f"duplicate address 0x{addr:08X} ({_clip(reg_name)!r} and "
                                f"{_clip(seen_addr[addr])!r})")
            seen_addr[addr] = reg_name
            if not in_peripheral_space(addr):
                in_spec_space += 1

            reset_el = reg_el.find("reset")
            reset_hex = reset_el.get("hex") if reset_el is not None else None
            reg_reset = _parse_hex(reset_hex) if reset_el is not None else None
            if reset_el is not None and reg_reset is None:
                bad_reset.append(reg_name)
            elif reg_reset is not None and not 0 <= reg_reset <= 0xFF:
                report.add_each("reset outside a byte",
                                f"{_clip(reg_name)!r}: <reset hex> {_clip(reset_hex)!r} is "
                                f"outside one byte 0..0xFF — read as no reset")
                reg_reset = None
            dropped_reg.update(a for a in reg_el.attrib if a not in ("name", "address", "access"))
            dropped_reg.update(f"<{c.tag}>" for c in reg_el if c.tag not in ("bit", "reset"))

            # Collect bits (position, name, access); note dropped vendor attrs.
            bits: List[Tuple[int, str, str]] = []
            for bit_el in reg_el.findall("bit"):
                pos = _parse_int(bit_el.get("position"))
                if pos is None or not (0 <= pos <= 7):
                    report.add_each("bit position out of range",
                                    f"{_clip(reg_name)!r}: bit position "
                                    f"{_clip(bit_el.get('position'))!r} out of range 0-7 "
                                    f"— skipped")
                    continue
                if any(b[0] == pos for b in bits):
                    report.add_each("bit position listed twice",
                                    f"{_clip(reg_name)!r}: bit position {pos} is listed "
                                    f"twice — both kept")
                bit_name = bit_el.get("name", "?")
                bits.append((pos, bit_name, _access_of(
                    bit_el.get("access"), default_access, f"{reg_name}.{bit_name}",
                    access_audit)))
                for attr in bit_el.attrib:
                    if attr not in ("position", "name", "access"):
                        dropped.add(attr)
            report.raw_bit_count += len(bits)

            fields = self._make_fields(bits, reg_reset, coalesce, reg_name, report)
            registers.append(RegisterSpec(
                name=reg_name, offset=addr, abs_address=addr,
                reset=reg_reset,
                access=_access_of(reg_el.get("access"), default_access, reg_name,
                                  access_audit),
                dual_ranked=False, curr_offset=None, fields=fields))
            if reg_reset is None and not fields:
                report.add_each("no reset and no fields",
                                f"{_clip(reg_name)!r}: no reset and no fields")

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
            report.add(f"dropped vendor bit attributes (not modelled): "
                       f"{_listed(sorted(dropped))}")
        if dropped_reg:
            report.add(f"dropped register attributes and elements (not modelled): "
                       f"{_listed(sorted(dropped_reg))}")
        _report_access(report, access_audit)
        if bad_reset:
            report.add(f"{len(bad_reset)} register(s) with an unparseable <reset hex> read as "
                       f"no reset: {_listed(bad_reset, 3)}")
        report.settle()

        return PeripheralRegisterMap(
            name=name, registers=registers,
            meta={"source_format": self.format_name, "source_name": source_name}), report

    @staticmethod
    def _make_fields(bits: List[Tuple[int, str, str]], reg_reset: Optional[int],
                     coalesce: bool, reg_name: str, report: ImportReport) -> List[FieldSpec]:
        """Coalesce `base[i]` per-bit runs into ranged FieldSpecs. Singletons and
        non-contiguous index runs stay split, each named by its own index range (see
        `_field_from_run`). Field resets are sliced from the
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
            runs: List[List[Tuple[int, int, str]]] = []
            for m in members:
                if runs and m[0] == runs[-1][-1][0] - 1:
                    runs[-1].append(m)
                else:
                    runs.append([m])
            for n, run in enumerate(runs):
                out.append(_field_from_run(base, run, field_reset, report, reg_name,
                                           indexed=n > 0 or len(run) == 1))
        for pos, nm, acc in singles:
            out.append(FieldSpec(name=nm, hi=pos, lo=pos, reset=field_reset(pos, pos), access=acc))

        out.sort(key=lambda fs: -fs.hi)
        return out


def _field_from_run(base: str, run: List[Tuple[int, int, str]], field_reset,
                    report: ImportReport, reg_name: str, indexed: bool = False) -> FieldSpec:
    """One field from a run of `base[i]` bits. A run of one bit, and every run of `base`
    after the first, keeps its index range in its name (`gain[3]`, `gain[5:4]`), so no two
    fields of a register share a name."""
    hi = run[0][0]
    lo = run[-1][0]
    accesses = {acc for _, _, acc in run}
    if len(accesses) > 1:
        report.add_each("mixed access",
                        f"{_clip(reg_name)!r}: field {_clip(base)!r} has mixed access "
                        f"[{_listed([repr(_clip(a)) for a in sorted(accesses)])}] — "
                        f"using {_clip(run[0][2])!r}")
    name = base
    if indexed:
        first, last = run[0][1], run[-1][1]
        name = f"{base}[{first}]" if first == last else f"{base}[{first}:{last}]"
    return FieldSpec(name=name, hi=hi, lo=lo, reset=field_reset(hi, lo), access=run[0][2])


_MAX_REGISTER_BITS = 64         # the widest register; a field address is at most 18 digits
_MAX_DEFAULT_BITS = 256         # a default is read this wide to see whether bits are lost
_BITS_REF_RE = re.compile(
    r"^\s*(?P<reg>[^\[\]\s]+)\s*\[(?P<hi>\d{1,18})(?::(?P<lo>\d{1,18}))?\]\s*$")
# The children each importer reads; anything else on a register or field is reported as
# not modelled, by its own name, so nothing about a dialect is hard-coded beyond these.
_REG_READ = {"Name", "Address", "DefaultValue", "Access"}
_FIELD_READ = {"Name", "Address", "DefaultValue", "Access", "Description", "Values"}


_HEX_DIGITS = re.compile(r"[0-9A-Fa-fXx]+", re.ASCII)
_BIN_DIGITS = re.compile(r"[01Xx]+", re.ASCII)
# A digit -> the same-width digit of the known-bit mask: all ones, or zeros for `X`.
_KNOWN_HEX = str.maketrans({**{c: "F" for c in "0123456789ABCDEF"}, "X": "0"})
_KNOWN_BIN = str.maketrans({"0": "1", "1": "1", "X": "0"})
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
    # One conversion per number, linear in the digits: an `X` digit is 0 in the value and
    # unknown in the mask.
    valid, nibble = (_HEX_DIGITS, 16) if base_bits == 4 else (_BIN_DIGITS, 2)
    if not valid.fullmatch(digits):
        return 0, 0
    upper = digits.upper()
    value = int(upper.replace("X", "0"), nibble)
    known = int(upper.translate(_KNOWN_HEX if base_bits == 4 else _KNOWN_BIN), nibble)
    if digits[0].upper() != "X" and base_bits * len(digits) < width:
        known |= full & ~((1 << (base_bits * len(digits))) - 1)
    return value & full, known & full


def _all_unknown(text: str, fmt: str = "") -> bool:
    """True for a hex or binary default whose digits are all `X` (`0xXX`)."""
    digits, base_bits = _default_digits(text, fmt)
    return bool(base_bits and digits and set(digits.upper()) == {"X"})


def _note_default(report: "ImportReport", what: str, el) -> None:
    """Report a default whose reading was a guess or failed: an unknown `format`, text that
    is not a number, or 0s and 1s read as binary with no prefix or format."""
    d_el = el.find("DefaultValue")
    if d_el is None:
        return
    text, fmt = (d_el.text or "").strip(), d_el.get("format", "")
    if fmt.strip() and fmt.strip().lower() not in _FORMATS:
        report.add_each("default format",
                        f"{what}: default {_clip(text)!r} has format {_clip(fmt)!r}, which is "
                        f"not hex, bin or dec — read as nothing known")
    elif (text and text.upper() != "X" and not _all_unknown(text, fmt)
          and _parse_default(text, 64, fmt) == (0, 0)):
        report.add_each("default not a number",
                        f"{what}: default {_clip(text)!r} is not a number — read as nothing "
                        f"known")
    elif _ambiguous_binary(text, fmt):
        digits = text.replace("_", "")
        shown = f" ({int(digits, 2)})" if len(digits) <= 64 else ""
        report.add_each("default read as binary",
                        f"{what}: default {_clip(text)!r} read as binary{shown}; "
                        f"write 0b{_clip(text)} or format=\"dec\" if it is decimal")


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
        root = _xml_tolerant_parse(text, source_name)
        dropped: set = set()

        fields_by_reg: Dict[str, List[tuple]] = {}
        swapped: List[str] = []
        bad_values = 0
        access_audit: Dict[str, List[str]] = {}
        for f_el in root.iter("Field"):
            ref = _BITS_REF_RE.match(_text(f_el, "Address"))
            if not ref:
                report.add_each("field without a REG[hi:lo] address",
                                f"field {_clip(_text(f_el, 'Name'))!r}: no REG[hi:lo] "
                                f"address — skipped")
                continue
            hi = int(ref.group("hi"))
            lo = int(ref.group("lo")) if ref.group("lo") is not None else hi
            if lo > hi:
                hi, lo = lo, hi
                swapped.append(_text(f_el, "Name") or ref.group("reg"))
            if hi >= _MAX_REGISTER_BITS:
                report.add_each("field past the widest register",
                                f"field {_clip(_text(f_el, 'Name') or ref.group('reg'))!r}: bit "
                                f"{hi} is past the widest register ({_MAX_REGISTER_BITS} bits) "
                                f"— skipped")
                continue
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
            f_what = f"field {_clip(_text(f_el, 'Name') or ref.group('reg'))!r}"
            if f_dv is not None and _parse_default(_text(f_el, "DefaultValue") or None,
                                                   _MAX_DEFAULT_BITS, f_fmt)[0] >> (hi - lo + 1):
                report.add_each("field default wider than its field",
                                f"{f_what}: default {_clip(_text(f_el, 'DefaultValue'))!r} has "
                                f"bits above the field's {hi - lo + 1}-bit width — dropped")
            _note_default(report, f_what, f_el)
            fields_by_reg.setdefault(ref.group("reg"), []).append((
                hi, lo, _text(f_el, "Name") or ref.group("reg"),
                dv if dk == (1 << (hi - lo + 1)) - 1 else None,
                f_el.find("DefaultValue") is not None,
                _access_of(_text(f_el, "Access"), default_access,
                           _text(f_el, "Name") or ref.group("reg"), access_audit),
                _text(f_el, "Description"), enum))
            dropped.update(c.tag for c in f_el if c.tag not in _FIELD_READ)
            report.raw_bit_count += 1

        registers: List[RegisterSpec] = []
        seen_addr: Dict[int, str] = {}
        in_spec_space = 0
        unknown_reset = 0
        named = set()
        field_owners = set()               # names of the registers that took their fields
        for r_el in root.iter("Register"):
            name = _text(r_el, "Name")
            addr = _parse_int(_text(r_el, "Address") or None)
            if not name or addr is None:
                if r_el.find("Address") is not None:
                    report.add_each("missing address",
                                    f"register {_clip(name or '?')!r}: missing/invalid "
                                    f"address — skipped")
                continue
            named.add(name)
            if not 0 <= addr <= 0xFFFFFFFF:
                report.add_each("address outside 32 bits",
                                f"register {_clip(name)!r}: address "
                                f"{_clip(_text(r_el, 'Address'))!r} is outside 0..0xFFFFFFFF "
                                f"— skipped")
                continue
            dropped.update(c.tag for c in r_el if c.tag not in _REG_READ)
            fields = fields_by_reg.get(name, [])
            if name in field_owners:       # a name is a key: its fields belong to the first
                if fields:
                    report.add_each("duplicate register name",
                                    f"register {_clip(name)!r} at 0x{addr:08X} repeats a "
                                    f"register name: the fields named for it belong to the "
                                    f"first, so this one has none")
                fields = []
            field_owners.add(name)
            raw = _text(r_el, "DefaultValue")
            r_dv = r_el.find("DefaultValue")
            r_fmt = r_dv.get("format", "") if r_dv is not None else ""
            digits, d_bits = _default_digits(raw, r_fmt)
            if d_bits:
                from_default = d_bits * len(digits)
            else:
                dec = _parse_int(digits) if digits.isdigit() else None
                from_default = 0 if dec is None else dec.bit_length()
            # The widest of the default's digits and the fields, rounded up to 8, 16, 32
            # or 64 bits: a register is a power-of-two number of bytes.
            width = max([8, from_default] + [hi + 1 for hi, *_ in fields])
            width = min(_MAX_REGISTER_BITS, 1 << (width - 1).bit_length())
            if not in_peripheral_space(addr):
                in_spec_space += 1
                continue
            if addr % (width // 8):
                report.add_each("alignment",
                                f"{_clip(name)!r}: 0x{addr:08X} is not aligned to its {width}-bit "
                                f"width")
            reg_value, reg_known = _parse_default(raw or None, width, r_fmt)
            if _parse_default(raw or None, _MAX_DEFAULT_BITS, r_fmt)[0] >> width:
                report.add_each("register default wider than its register",
                                f"{_clip(name)!r}: default {_clip(raw)!r} has bits above the "
                                f"register's {width}-bit width — dropped")
            _note_default(report, f"register {_clip(name)!r}", r_el)
            if reg_known != (1 << width) - 1:
                unknown_reset += 1
            access = _access_of(_text(r_el, "Access"), default_access, name, access_audit)
            for k in range(width // 8):
                b_lo, b_hi = 8 * k, 8 * k + 7
                b_addr = addr + k
                b_name = name if width == 8 else f"{name}[{b_hi}:{b_lo}]"
                if b_addr in seen_addr:
                    report.add_each("duplicate address",
                                    f"duplicate address 0x{b_addr:08X} ({_clip(b_name)!r} and "
                                    f"{_clip(seen_addr[b_addr])!r})")
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

        if swapped:
            report.add(f"{len(swapped)} field(s) written low bit first read as [hi:lo]: "
                       f"{_listed(swapped, 3)}")
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
        _report_access(report, access_audit)
        report.settle()
        if unknown_reset:
            report.add(f"{unknown_reset} register(s) with an unknown reset in part or whole: "
                       f"those bytes take their fields' resets")
        if dropped:
            report.add(f"not modelled: {_listed(sorted(dropped))}")
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


def decode_text(data: bytes) -> Tuple[str, List[str]]:
    """(text, notes) of a register-map file's bytes. A byte-order mark (UTF-8, UTF-16,
    UTF-32) or valid UTF-8 decides; otherwise the XML declaration's encoding, then
    Windows-1252, then Latin-1. A file that was not UTF-8 says which encoding it was read
    as, so a wrong guess shows in the import warnings."""
    boms = ((codecs.BOM_UTF32_LE, "utf-32"), (codecs.BOM_UTF32_BE, "utf-32"),
            (codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"),
            (codecs.BOM_UTF16_BE, "utf-16"))
    tries: List[str] = [enc for bom, enc in boms if data.startswith(bom)][:1]
    tries.append("utf-8")
    decl = re.match(rb"\s*<\?xml[^>]*encoding\s*=\s*[\"']([\w.:-]+)[\"']", data)
    if decl:
        tries.append(decl.group(1).decode("ascii"))
    tries += ["cp1252", "latin-1"]
    for enc in tries:
        try:
            text = data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
        if enc in ("utf-8", "utf-8-sig", "utf-16", "utf-32"):
            return text, []
        return text, [f"the file is not UTF-8: read as {enc}"]
    raise AssertionError("latin-1 decodes any bytes")      # pragma: no cover


# The largest register-map file read, in bytes: a map is a few MB at most, so a larger file
# is not one.
MAX_REGMAP_BYTES = 64 << 20


def read_text(path: str) -> Tuple[str, List[str]]:
    """(text, notes) of a register-map file, as `decode_text` reads its bytes. ValueError
    naming the file when it is over MAX_REGMAP_BYTES; a device or pipe is read no further
    than the limit."""
    with open(path, "rb") as f:
        data = f.read(MAX_REGMAP_BYTES + 1)
    if len(data) > MAX_REGMAP_BYTES:
        raise ValueError(f"{os.path.basename(str(path))}: too large for a register map "
                         f"(over {MAX_REGMAP_BYTES:,} bytes)")
    return decode_text(data)


def import_file(path: str, coalesce: bool = True, default_access: str = "") \
        -> Tuple[PeripheralRegisterMap, ImportReport]:
    text, notes = read_text(path)
    pmap, report = import_text(text, os.path.splitext(os.path.basename(path))[0],
                               coalesce, default_access)
    report.warnings[:0] = notes
    return pmap, report


def library_dir() -> str:
    """Path to the bundled register-map library (data/regmaps), across dev,
    installed (pip data-files), and PyInstaller layouts."""
    return bundled_data_path("regmaps")
