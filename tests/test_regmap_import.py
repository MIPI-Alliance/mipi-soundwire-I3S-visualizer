"""Peripheral register-map import + per-device decode (Feature B).

Self-contained: uses an inline bit-list XML fixture (rootless, indexed bits,
vendor attributes, a duplicate address, and one register in SWI3S spec space)
to exercise tolerant parsing, field coalescing, the sanity report, JSON
round-trip, the bundled library example, and per-device resolve routing.

Run: python3 -m pytest tests/test_regmap_import.py
"""
import os
import pathlib

from swi3s_studio.model.registers import Provenance, RegisterMap, apply_commands
from swi3s_studio.model.regmap_import import (
    PeripheralRegisterMap,
    import_text,
    in_peripheral_space,
    library_dir,
)

# Rootless vendor XML (no enclosing element) with the awkward bits we must handle.
FIXTURE = """<header version="1">
  <option width="4" />
</header>
<register name="DEV_CTRL" address="0x10000100">
  <bit position="0" name="soft_rst" access="RW" locked="N" group="A" />
  <reset hex="0x00" />
</register>
<register name="PWR_CTRL" address="0x10000101">
  <bit position="6" name="out_en" access="RW" locked="N" />
  <bit position="3" name="src_sel[1]" access="RW" />
  <bit position="2" name="src_sel[0]" access="RW" />
  <bit position="0" name="ref_en" access="RW" />
  <reset hex="0x40" />
</register>
<register name="GAIN" address="0x10000104">
  <bit position="5" name="gain[5]" access="RW" />
  <bit position="4" name="gain[4]" access="RW" />
  <bit position="3" name="gain[3]" access="RW" />
  <bit position="2" name="gain[2]" access="RW" />
  <bit position="1" name="gain[1]" access="RW" />
  <bit position="0" name="gain[0]" access="RW" />
  <reset hex="0x15" />
</register>
<register name="CTRL_A" address="0x1000010A">
  <bit position="0" name="a" access="RW" />
  <reset hex="0x00" />
</register>
<register name="CTRL_B_DUP" address="0x1000010A">
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
    assert rep.format == "bit-list-xml"
    assert rep.register_count == 6            # rootless XML parsed; all 6 registers
    assert rep.address_min == 0x00001081 and rep.address_max == 0x1000010A
    assert in_peripheral_space(0x10000101) and not in_peripheral_space(0x00001081)


def test_coalescing_and_singletons():
    pmap, _ = _imported()
    amp = pmap.resolve(0x10000104)
    # gain[5..0] coalesces into one ranged field.
    gain = [f for f in amp.fields if f.name == "gain"]
    assert len(gain) == 1 and (gain[0].hi, gain[0].lo) == (5, 0)
    en = pmap.resolve(0x10000101)
    names = {f.name: (f.hi, f.lo) for f in en.fields}
    assert names["src_sel"] == (3, 2)        # indexed pair coalesced
    assert names["out_en"] == (6, 6)          # singleton preserved
    assert names["ref_en"] == (0, 0)


def test_field_reset_sliced_from_register():
    pmap, _ = _imported()
    amp = pmap.resolve(0x10000104)
    gain = next(f for f in amp.fields if f.name == "gain")
    assert amp.reset == 0x15 and gain.reset == 0x15     # [5:0] of 0x15 = 21
    en = pmap.resolve(0x10000101)
    assert next(f for f in en.fields if f.name == "out_en").reset == 1   # bit6 of 0x40


def test_no_coalesce_keeps_bits_split():
    pmap, _ = import_text(FIXTURE, "fixture", coalesce=False)
    amp = pmap.resolve(0x10000104)
    assert len(amp.fields) == 6 and all(f.hi == f.lo for f in amp.fields)


def test_report_warnings_and_dropped_attrs():
    _pmap, rep = _imported()
    assert "locked" in rep.dropped_attributes and "group" in rep.dropped_attributes
    blob = " ".join(rep.warnings)
    assert "duplicate address 0x1000010a" in blob.lower()
    assert "outside device-defined space" in blob          # the STRAY_SPEC_REG @0x1081


def test_json_roundtrip():
    pmap, _ = _imported()
    back = PeripheralRegisterMap.from_json(pmap.to_json())
    assert len(back.registers) == len(pmap.registers)
    a = back.resolve(0x10000104)
    assert [(f.name, f.hi, f.lo) for f in a.fields] == \
           [(f.name, f.hi, f.lo) for f in pmap.resolve(0x10000104).fields]


def test_library_example_loads():
    path = os.path.join(library_dir(), "example_amp.json")
    assert os.path.exists(path), path
    pmap = PeripheralRegisterMap.from_json(pathlib.Path(path).read_text(encoding="utf-8"))
    assert pmap.resolve(0x10000104).reset == 0x15


def test_per_device_resolve_routing():
    pmap, _ = _imported()
    rmap = RegisterMap.load()
    # One WriteA32 to a peripheral register (dev0) and one to a SWI3S DP reg (dev1).
    cmds = [
        {"command": "WriteA32", "crc_valid": True, "has_address": True,
         "address": 0x10000104, "data": bytes([0x2A]), "device_mask": 0b01, "start_sample": 0},
        {"command": "WriteA32", "crc_valid": True, "has_address": True,
         "address": 0x2090, "data": bytes([0x03]), "device_mask": 0b10, "start_sample": 1},
    ]
    files = apply_commands(rmap, cmds, peripheral_maps={0: pmap})
    # dev0: the peripheral write is captured, and an unwritten peripheral reg shows reset.
    assert files[0].value(0x10000104) == 0x2A
    assert files[0].provenance(0x10000104) == Provenance.WRITTEN
    assert files[0].value(0x10000101) == 0x40          # PWR_CTRL reset via the bound map
    # dev1 has no peripheral map: device-defined address falls back to 0, SWI3S space intact.
    assert files[1].value(0x2090) == 0x03
    assert files[1].value(0x10000104) == 0


# ---- register + field lists: wide registers split into the bytes the bus addresses ----
#
# A synthetic map in the second shape: registers listed with their full width, fields
# listed apart and naming their register and bits. Invented names; no real device.
WIDE_FIXTURE = """<?xml version="1.0" encoding="US-ASCII"?>
<DeviceMap>
  <Registers>
    <Register>
      <Name>AMP_CTRL</Name><Address>0x10000100</Address>
      <DefaultValue format="hex">0x00A50003</DefaultValue><Access>R/W</Access><Extra>1</Extra>
    </Register>
    <Register>
      <Name>STATUS</Name><Address>0x10000104</Address>
      <DefaultValue>XXXX_XXXX_XXXX_XXXX_XXXX_XXXX_XXXX_XXXX</DefaultValue><Access>R/O</Access>
    </Register>
    <Register>
      <Name>SPEC_COPY</Name><Address>0x1000</Address>
      <DefaultValue>0x00000000</DefaultValue><Access>R/O</Access>
    </Register>
  </Registers>
  <Fields>
    <Field>
      <Name>AMP_EN</Name><Address>AMP_CTRL[0]</Address><Description>Amplifier enable</Description>
      <DefaultValue>0x1</DefaultValue><Access>R/W</Access>
      <Values>
        <Value Value="0x0"><Description>Off</Description></Value>
        <Value Value="0x1"><Description>On</Description></Value>
      </Values>
    </Field>
    <Field><Name>MODE</Name><Address>AMP_CTRL[2:1]</Address><DefaultValue>0x1</DefaultValue>
      <Access>R/W</Access></Field>
    <Field><Name>LEVEL</Name><Address>AMP_CTRL[23:12]</Address><DefaultValue>0xA50</DefaultValue>
      <Access>R/W</Access><Values><Value Value="0x0"><Description>Mute</Description></Value></Values>
    </Field>
    <Field><Name>KICK</Name><Address>AMP_CTRL[31]</Address><DefaultValue>0x0</DefaultValue>
      <Access>W/O</Access></Field>
    <Field><Name>BUSY</Name><Address>STATUS[0]</Address><DefaultValue>X</DefaultValue>
      <Access>R/O</Access></Field>
    <Field><Name>READY</Name><Address>STATUS[1]</Address><DefaultValue>0x0</DefaultValue>
      <Access>R/O</Access></Field>
    <Field><Name>ID</Name><Address>SPEC_COPY[7:0]</Address><DefaultValue>0x0</DefaultValue>
      <Access>R/O</Access></Field>
    <Field><Name>LOST</Name><Address>NOWHERE[3:0]</Address><DefaultValue>0x0</DefaultValue>
      <Access>R/O</Access></Field>
  </Fields>
</DeviceMap>
"""


def _wide():
    return import_text(WIDE_FIXTURE, "wide")


def _fields(reg):
    return {f.name: f for f in reg.fields}


def test_a_register_field_map_is_recognised_by_its_shape():
    """Nothing about the root element or any vendor is matched: the register list, the
    field list and REG[hi:lo] addresses are the format."""
    pmap, rep = _wide()
    assert rep.format == "register-field-xml" and rep.ok
    assert pmap.meta["source_format"] == "register-field-xml"


def test_each_wide_register_becomes_the_bytes_the_bus_addresses_little_endian():
    pmap, rep = _wide()
    assert [r.abs_address for r in pmap.registers] == list(range(0x10000100, 0x10000108))
    assert [r.name for r in pmap.registers][:4] == ["AMP_CTRL[7:0]", "AMP_CTRL[15:8]",
                                                    "AMP_CTRL[23:16]", "AMP_CTRL[31:24]"]
    # 0x00A50003: byte 0 is 0x03, byte 2 is 0xA5.
    assert [pmap.resolve(0x10000100 + k).reset for k in range(4)] == [0x03, 0x00, 0xA5, 0x00]
    assert pmap.resolve(0x10000100).access == "RW" and pmap.resolve(0x10000104).access == "RO"


def test_a_field_inside_one_byte_keeps_its_name_labels_and_description():
    pmap, _ = _wide()
    b0 = _fields(pmap.resolve(0x10000100))
    assert (b0["AMP_EN"].hi, b0["AMP_EN"].lo, b0["AMP_EN"].reset) == (0, 0, 1)
    assert b0["AMP_EN"].enum == {0: "Off", 1: "On"}
    assert b0["AMP_EN"].description == "Amplifier enable"
    assert (b0["MODE"].hi, b0["MODE"].lo, b0["MODE"].reset) == (2, 1, 1)
    kick = _fields(pmap.resolve(0x10000103))["KICK"]
    assert (kick.hi, kick.lo, kick.access) == (7, 7, "WO")


def test_a_field_across_bytes_is_sliced_one_piece_per_byte_named_by_its_own_bits():
    """LEVEL is AMP_CTRL[23:12], reset 0xA50: its low four bits sit in byte 1 (bits 7:4)
    and the rest is byte 2. Value labels describe the whole field, so pieces carry none."""
    pmap, _ = _wide()
    lo_piece = _fields(pmap.resolve(0x10000101))["LEVEL[3:0]"]
    hi_piece = _fields(pmap.resolve(0x10000102))["LEVEL[11:4]"]
    assert (lo_piece.hi, lo_piece.lo, lo_piece.reset) == (7, 4, 0x0)
    assert (hi_piece.hi, hi_piece.lo, hi_piece.reset) == (7, 0, 0xA5)
    assert not lo_piece.enum and not hi_piece.enum


def test_unknown_reset_bits_are_unknown_not_zero():
    pmap, rep = _wide()
    b0 = pmap.resolve(0x10000104)
    assert b0.reset is None
    f = _fields(b0)
    assert f["BUSY"].reset is None and f["READY"].reset == 0
    assert any("unknown reset" in w for w in rep.warnings)


def test_spec_space_registers_and_orphan_fields_are_skipped_and_said():
    pmap, rep = _wide()
    assert pmap.resolve(0x1000) is None
    assert any("1 register(s) outside device-defined space" in w for w in rep.warnings)
    assert any("1 field(s) name a register the file does not define" in w
               for w in rep.warnings)
    assert "Extra" in rep.dropped_attributes


def test_labels_and_descriptions_survive_the_normalized_json():
    """The map is embedded in a saved workspace as normalized JSON; a field's value labels
    and description come back with it (older maps, which have neither, read as before)."""
    pmap, _ = _wide()
    back = PeripheralRegisterMap.from_json(pmap.to_json())
    f = _fields(back.resolve(0x10000100))["AMP_EN"]
    assert f.enum == {0: "Off", 1: "On"} and f.description == "Amplifier enable"
    old = PeripheralRegisterMap.from_json(_imported()[0].to_json())
    assert all(not fs.enum and not fs.description for r in old.registers for fs in r.fields)


# A short default is a number: the digits not written are zeros, unless the leftmost digit
# written is X, which makes them unknown too.
SHORT_DEFAULTS_FIXTURE = """<?xml version="1.0" encoding="US-ASCII"?>
<DeviceMap>
  <Registers>
    <Register><Name>CTRL</Name><Address>0x10000200</Address>
      <DefaultValue>0x50041</DefaultValue><Access>R/W</Access></Register>
    <Register><Name>HALF</Name><Address>0x10000204</Address>
      <DefaultValue>0xX1</DefaultValue><Access>R/W</Access></Register>
  </Registers>
  <Fields>
    <Field><Name>MODE</Name><Address>CTRL[4:0]</Address><DefaultValue>0x1</DefaultValue>
      <Access>R/W</Access></Field>
    <Field><Name>GAIN</Name><Address>CTRL[11:5]</Address><DefaultValue>10</DefaultValue>
      <Access>R/W</Access></Field>
    <Field><Name>LVL</Name><Address>CTRL[23:12]</Address><Access>R/W</Access></Field>
    <Field><Name>KICK</Name><Address>CTRL[31:24]</Address><DefaultValue>0x0</DefaultValue>
      <Access>R/W</Access></Field>
    <Field><Name>LOW</Name><Address>HALF[3:0]</Address><Access>R/W</Access></Field>
    <Field><Name>TOP</Name><Address>HALF[15:8]</Address><Access>R/W</Access></Field>
  </Fields>
</DeviceMap>
"""


def test_a_default_is_read_by_its_format_attribute_or_prefix():
    """format="dec" / "hex" / "bin" decides; without it `0x` is hex, `0b` binary, and digits of
    0 and 1 alone binary. An unknown format leaves the default unknown."""
    from swi3s_studio.model.regmap_import import _parse_default
    assert _parse_default("10", 8, "dec") == (10, 0xFF)
    assert _parse_default("1F", 8, "hex") == (0x1F, 0xFF)
    assert _parse_default("0x1F", 8, "hex") == (0x1F, 0xFF)
    assert _parse_default("101", 8, "bin") == (5, 0xFF)
    assert _parse_default("0b10", 8) == (2, 0xFF)
    assert _parse_default("41", 8) == (41, 0xFF)
    assert _parse_default("10", 8, "octal") == (0, 0)


FORMAT_FIXTURE = """<?xml version="1.0" encoding="US-ASCII"?>
<DeviceMap>
  <Registers>
    <Register><Name>CTRL</Name><Address>0x10000300</Address>
      <DefaultValue format="dec">10</DefaultValue><Access>R/W</Access></Register>
  </Registers>
  <Fields>
    <Field><Name>LOW</Name><Address>CTRL[3:0]</Address>
      <DefaultValue format="dec">10</DefaultValue><Access>R/W</Access></Field>
    <Field><Name>HIGH</Name><Address>CTRL[7:4]</Address><DefaultValue>0</DefaultValue>
      <Access>R/W</Access></Field>
  </Fields>
</DeviceMap>
"""


def test_a_decimal_default_imports_by_its_format_and_a_guess_is_reported():
    pmap, rep = import_text(FORMAT_FIXTURE, "fmt")
    reg = pmap.resolve(0x10000300)
    assert reg.reset == 10 and _fields(reg)["LOW"].reset == 10
    assert not any("read as binary" in w for w in rep.warnings), rep.warnings
    _pmap, rep = import_text(SHORT_DEFAULTS_FIXTURE, "short")
    assert any(w.startswith("field 'GAIN': default '10' read as binary (2)")
               for w in rep.warnings), rep.warnings
    _pmap, rep = import_text(FORMAT_FIXTURE.replace(">0</DefaultValue>", ">zz</DefaultValue>"),
                             "bad")
    assert "field 'HIGH': default 'zz' is not a number — read as nothing known" in rep.warnings


def test_a_default_shorter_than_its_width_has_known_zeros_above_its_digits():
    from swi3s_studio.model.regmap_import import _parse_default
    assert _parse_default("0x0", 32) == (0, 0xFFFFFFFF)
    assert _parse_default("0x1", 12) == (1, 0xFFF)
    assert _parse_default("10", 8) == (0b10, 0xFF)
    assert _parse_default("0xX1", 16) == (1, 0x000F)
    assert _parse_default("X1", 8) == (1, 0x01)
    pmap, rep = import_text(SHORT_DEFAULTS_FIXTURE, "short")
    assert [pmap.resolve(0x10000200 + k).reset for k in range(4)] == [0x41, 0x00, 0x05, 0x00]
    b0, b1, b2 = (_fields(pmap.resolve(0x10000200 + k)) for k in range(3))
    assert b0["MODE"].reset == 1 and b0["GAIN[2:0]"].reset == 0b010
    assert b1["GAIN[6:3]"].reset == 0 and b1["LVL[3:0]"].reset == 0
    assert b2["LVL[11:4]"].reset == 0x05
    assert _fields(pmap.resolve(0x10000203))["KICK"].reset == 0
    # HALF: only bits 3:0 are known, so its bytes are unknown but LOW still takes 1.
    lo, hi = pmap.resolve(0x10000204), pmap.resolve(0x10000205)
    assert lo.reset is None and hi.reset is None
    assert _fields(lo)["LOW"].reset == 1 and _fields(hi)["TOP"].reset is None
    assert any("1 register(s) with an unknown reset" in w for w in rep.warnings), rep.warnings


# Registers whose default is written short and whose top field ends below bit 24: the
# inferred width rounds up to a power of two, so each is a whole word.
SHORT_WORD_FIXTURE = """<?xml version="1.0" encoding="US-ASCII"?>
<DeviceMap>
  <Registers>
    <Register><Name>R</Name><Address>0x10000100</Address>
      <DefaultValue>0x0</DefaultValue><Access>R/W</Access></Register>
    <Register><Name>S</Name><Address>0x10000204</Address>
      <DefaultValue>0x050041</DefaultValue><Access>R/W</Access></Register>
    <Register><Name>T</Name><Address>0x10000300</Address>
      <DefaultValue>0x0</DefaultValue><Access>R/W</Access></Register>
  </Registers>
  <Fields>
    <Field><Name>LVL</Name><Address>R[23:16]</Address><Access>R/W</Access></Field>
    <Field><Name>A</Name><Address>S[19:0]</Address><Access>R/W</Access></Field>
    <Field><Name>W</Name><Address>T[39:32]</Address><Access>R/W</Access></Field>
  </Fields>
</DeviceMap>
"""


def test_an_inferred_width_rounds_up_to_a_power_of_two_bytes():
    pmap, rep = import_text(SHORT_WORD_FIXTURE, "short-word")
    addrs = [r.abs_address for r in pmap.registers]
    assert addrs[:4] == list(range(0x10000100, 0x10000104))
    assert addrs[4:8] == list(range(0x10000204, 0x10000208))
    assert addrs[8:] == list(range(0x10000300, 0x10000308))     # bit 39: 64 bits
    assert pmap.resolve(0x10000103).name == "R[31:24]"
    assert [pmap.resolve(0x10000204 + k).reset for k in range(4)] == [0x41, 0x00, 0x05, 0x00]
    assert not any("not aligned" in w for w in rep.warnings), rep.warnings
