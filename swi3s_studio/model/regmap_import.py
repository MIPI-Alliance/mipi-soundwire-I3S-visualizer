"""Peripheral (chip-specific) register-map import.

SWI3S Studio decodes the SWI3S spec registers from `data/registers.json`. Each
peripheral on the bus can also be a different chip with its own vendor register
map in the device-defined address space (0x10000000-0x3FFFFFFF local). This
module imports such vendor maps into the *same* RegisterSpec/FieldSpec model the
register view already renders, so no UI or replay code needs to change.

It is deliberately an IMPORT layer, not a new canonical format:

* `RegisterMapImporter` is the interface; `CirrusXmlImporter` handles the
  Cirrus-style `<register>`/`<bit>` XML (the first real-world dialect).
* Import is tolerant: it wraps the rootless vendor XML in a synthetic root,
  strips a BOM / XML declaration, and escapes stray ampersands.
* Per-bit `name[i]` runs are coalesced back into ranged fields
  (`amp_lvl[5]..[0]` -> one `amp_lvl[5:0]` FieldSpec).
* Every import returns an `ImportReport` describing what was inferred and what
  looked off (address-space class, duplicates, out-of-range bits, missing
  resets, dropped vendor attributes) so the assistant can show it before commit.

The normalized result (`PeripheralRegisterMap`) round-trips to a small JSON so a
converted map can be cached in the workspace / `data/regmaps/` and re-opened
without the vendor file.
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .registers import FieldSpec, RegisterSpec, _parse_int

logger = logging.getLogger(__name__)

# Normalized-JSON schema tag (bump if the on-disk shape changes).
REGMAP_SCHEMA = "swi3s-studio-regmap/1"

# Local device-defined peripheral space (spec Table 163). Remote mirror too.
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
    address_min: Optional[int] = None
    address_max: Optional[int] = None
    warnings: List[str] = field(default_factory=list)
    dropped_attributes: List[str] = field(default_factory=list)  # e.g. ['secure', 'limitation']

    def add(self, msg: str) -> None:
        self.warnings.append(msg)

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
                    "fields": [
                        {
                            "name": fs.name,
                            "bits": f"[{fs.hi}:{fs.lo}]" if fs.hi != fs.lo else f"[{fs.hi}]",
                            "reset": None if fs.reset is None else int(fs.reset),
                            "access": fs.access,
                        }
                        for fs in r.fields
                    ],
                }
                for r in self.registers
            ],
        }

    @classmethod
    def from_json_dict(cls, doc: dict) -> "PeripheralRegisterMap":
        meta = dict(doc.get("_meta", {}))
        name = meta.pop("name", "") if isinstance(meta, dict) else ""
        meta.pop("schema", None)
        regs: List[RegisterSpec] = []
        seen_addr: Dict[int, str] = {}
        for r in doc.get("registers", []):
            addr = _parse_int(r.get("address"))
            if addr is None:                        # unparseable/missing address:
                continue                            # skip rather than collapse to 0 (key clash)
            if addr in seen_addr:
                # resolve()/_by_addr keeps only the last register at an address, so a
                # duplicate silently shadows the earlier one -- warn like the XML path.
                logger.warning("regmap %r: duplicate address 0x%08X (%r and %r); "
                               "the later register wins on resolve",
                               name, addr, seen_addr[addr], r.get("name", "?"))
            seen_addr[addr] = r.get("name", "?")
            fields: List[FieldSpec] = []
            for fd in r.get("fields", []):
                bits = str(fd.get("bits", ""))      # tolerate a non-string in cached JSON
                m = re.match(r"\[(\d+)(?::(\d+))?\]", bits.strip())
                if not m:
                    logger.warning("regmap %r: register %r field %r has an unparseable "
                                   "bits spec %r; dropped", name, r.get("name", "?"),
                                   fd.get("name", "?"), bits)
                    continue
                hi = int(m.group(1))
                lo = int(m.group(2)) if m.group(2) is not None else hi
                fields.append(FieldSpec(
                    name=fd.get("name", "?"), hi=hi, lo=lo,
                    reset=_parse_int(fd.get("reset")), access=fd.get("access", "")))
            regs.append(RegisterSpec(
                name=r.get("name", "?"), offset=addr, abs_address=addr,
                reset=_parse_int(r.get("reset")), access=r.get("access", ""),
                dual_ranked=False, curr_offset=None, fields=fields))
        return cls(name=name, registers=regs, meta=meta)

    def to_json(self) -> str:
        return json.dumps(self.to_json_dict(), indent=2)

    @classmethod
    def from_json(cls, text: str) -> "PeripheralRegisterMap":
        return cls.from_json_dict(json.loads(text))


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


class CirrusXmlImporter(RegisterMapImporter):
    """Cirrus-style `<register name addr><bit position name access/>...<reset hex/></register>`."""
    format_name = "cirrus-xml"

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

            # Collect bits (position, name, access); note dropped vendor attrs.
            bits: List[Tuple[int, str, str]] = []
            for bit_el in reg_el.findall("bit"):
                pos = _parse_int(bit_el.get("position"))
                if pos is None or not (0 <= pos <= 7):
                    report.add(f"{reg_name!r}: bit position {bit_el.get('position')!r} out of range 0-7 — skipped")
                    continue
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


# Registry — first importer whose sniff() matches wins.
_IMPORTERS: List[type] = [CirrusXmlImporter]


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
