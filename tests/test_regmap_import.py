"""Peripheral register-map import + per-device decode (Feature B).

Self-contained: uses an inline Cirrus-style fixture (rootless, indexed bits,
vendor attributes, a duplicate address, and one register in SWI3S spec space)
to exercise tolerant parsing, field coalescing, the sanity report, JSON
round-trip, the bundled library example, and per-device resolve routing.

Run: python3 -m pytest tests/test_regmap_import.py
"""
import os

from swi3s_studio.model.registers import Provenance, RegisterMap, apply_commands
from swi3s_studio.model.regmap_import import (
    PeripheralRegisterMap,
    import_text,
    in_peripheral_space,
    library_dir,
)

# Rootless vendor XML (no enclosing element) with the awkward bits we must handle.
FIXTURE = """<config schema_version="2">
  <param addr_bytes="4" />
</config>
<register name="SW_RESET" address="0x10000001">
  <bit position="0" name="sw_reset" access="RW" secure="N" limitation="O" />
  <reset hex="0x00" />
</register>
<register name="ENABLE" address="0x10000002">
  <bit position="7" name="amp_en" access="RW" secure="N" />
  <bit position="5" name="chan_sel[1]" access="RW" />
  <bit position="4" name="chan_sel[0]" access="RW" />
  <bit position="0" name="boost_en" access="RW" />
  <reset hex="0x80" />
</register>
<register name="AMP_LEVEL" address="0x10000007">
  <bit position="5" name="amp_lvl[5]" access="RW" />
  <bit position="4" name="amp_lvl[4]" access="RW" />
  <bit position="3" name="amp_lvl[3]" access="RW" />
  <bit position="2" name="amp_lvl[2]" access="RW" />
  <bit position="1" name="amp_lvl[1]" access="RW" />
  <bit position="0" name="amp_lvl[0]" access="RW" />
  <reset hex="0x0A" />
</register>
<register name="CTRL_A" address="0x1000000A">
  <bit position="0" name="a" access="RW" />
  <reset hex="0x00" />
</register>
<register name="CTRL_B_DUP" address="0x1000000A">
  <bit position="0" name="b" access="R" />
  <reset hex="0x00" />
</register>
<register name="STRAY_SPEC_REG" address="0x00001081">
  <bit position="0" name="x" access="RW" />
  <reset hex="0x00" />
</register>
"""


def _imported():
    return import_text(FIXTURE, "fixture", coalesce=True)


def test_tolerant_parse_and_counts():
    pmap, rep = _imported()
    assert rep.format == "cirrus-xml"
    assert rep.register_count == 6            # rootless XML parsed; all 6 registers
    assert rep.address_min == 0x00001081 and rep.address_max == 0x1000000A
    assert in_peripheral_space(0x10000002) and not in_peripheral_space(0x00001081)


def test_coalescing_and_singletons():
    pmap, _ = _imported()
    amp = pmap.resolve(0x10000007)
    # amp_lvl[5..0] coalesces into one ranged field.
    amp_lvl = [f for f in amp.fields if f.name == "amp_lvl"]
    assert len(amp_lvl) == 1 and (amp_lvl[0].hi, amp_lvl[0].lo) == (5, 0)
    en = pmap.resolve(0x10000002)
    names = {f.name: (f.hi, f.lo) for f in en.fields}
    assert names["chan_sel"] == (5, 4)        # indexed pair coalesced
    assert names["amp_en"] == (7, 7)          # singleton preserved
    assert names["boost_en"] == (0, 0)


def test_field_reset_sliced_from_register():
    pmap, _ = _imported()
    amp = pmap.resolve(0x10000007)
    amp_lvl = next(f for f in amp.fields if f.name == "amp_lvl")
    assert amp.reset == 0x0A and amp_lvl.reset == 0x0A     # [5:0] of 0x0A = 10
    en = pmap.resolve(0x10000002)
    assert next(f for f in en.fields if f.name == "amp_en").reset == 1   # bit7 of 0x80


def test_no_coalesce_keeps_bits_split():
    pmap, _ = import_text(FIXTURE, "fixture", coalesce=False)
    amp = pmap.resolve(0x10000007)
    assert len(amp.fields) == 6 and all(f.hi == f.lo for f in amp.fields)


def test_report_warnings_and_dropped_attrs():
    _pmap, rep = _imported()
    assert "secure" in rep.dropped_attributes and "limitation" in rep.dropped_attributes
    blob = " ".join(rep.warnings)
    assert "duplicate address 0x1000000a" in blob.lower()
    assert "outside device-defined space" in blob          # the STRAY_SPEC_REG @0x1081


def test_json_roundtrip():
    pmap, _ = _imported()
    back = PeripheralRegisterMap.from_json(pmap.to_json())
    assert len(back.registers) == len(pmap.registers)
    a = back.resolve(0x10000007)
    assert [(f.name, f.hi, f.lo) for f in a.fields] == \
           [(f.name, f.hi, f.lo) for f in pmap.resolve(0x10000007).fields]


def test_library_example_loads():
    path = os.path.join(library_dir(), "example_amp.json")
    assert os.path.exists(path), path
    pmap = PeripheralRegisterMap.from_json(open(path, encoding="utf-8").read())
    assert pmap.resolve(0x10000007).reset == 0x0A


def test_per_device_resolve_routing():
    pmap, _ = _imported()
    rmap = RegisterMap.load()
    # One WriteA32 to a peripheral register (dev0) and one to a SWI3S DP reg (dev1).
    cmds = [
        {"command": "WriteA32", "crc_valid": True, "has_address": True,
         "address": 0x10000007, "data": bytes([0x2A]), "device_mask": 0b01, "start_sample": 0},
        {"command": "WriteA32", "crc_valid": True, "has_address": True,
         "address": 0x2090, "data": bytes([0x03]), "device_mask": 0b10, "start_sample": 1},
    ]
    files = apply_commands(rmap, cmds, peripheral_maps={0: pmap})
    # dev0: the peripheral write is captured, and an unwritten peripheral reg shows reset.
    assert files[0].value(0x10000007) == 0x2A
    assert files[0].provenance(0x10000007) == Provenance.WRITTEN
    assert files[0].value(0x10000002) == 0x80          # ENABLE reset via the bound map
    # dev1 has no peripheral map: device-defined address falls back to 0, SWI3S space intact.
    assert files[1].value(0x2090) == 0x03
    assert files[1].value(0x10000007) == 0
