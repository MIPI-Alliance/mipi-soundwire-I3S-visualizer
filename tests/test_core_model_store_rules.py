"""Rules of the model and store layers: a flow-controlled PDM port's native rate, the
register-map JSON reader and importers, the store package's imports, and the command
store's register-write feed."""
import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from swi3s_studio import import_notes
from swi3s_studio.model.regmap_import import (
    PeripheralRegisterMap,
    decode_text,
    import_file,
    import_text,
)
from swi3s_studio.store.audio_store import AudioStore

pytestmark = pytest.mark.platform

# ---- flow-controlled PDM native rate ----

_CAP = 24_576_000.0
_BIT_RATE = 3_072_000.0


def _pdm_store(flow_mode, core_rate=_BIT_RATE, jitter=False):
    """A PDM port of 128000 bits at `_BIT_RATE`. With `jitter` the bits' transport positions
    alternate 6 and 10 capture samples apart (8 on average) and stop for 5000 capture
    samples half way, as a flow-controlled port's do."""
    n = 64 * 2000
    rng = np.random.default_rng(3)
    steps = np.full(n, 8, np.int64)
    if jitter:
        steps[0::2], steps[1::2] = 6, 10
        steps[n // 2] += 5000
    cols = {
        "device": np.zeros(n, np.int32), "dp": np.zeros(n, np.int32),
        "channel": np.zeros(n, np.int32), "sample_size": np.ones(n, np.int32),
        "value": rng.integers(0, 2, n).astype(np.uint32),
        "index": np.arange(n, dtype=np.uint64),
        "start_sample": np.cumsum(steps).astype(np.uint64),
        "flow_mode": np.full(n, flow_mode, np.int32),
    }
    return AudioStore.from_audio_columns(cols, {(0, 0): core_rate}, capture_rate_hz=_CAP)


@pytest.mark.parametrize("flow_mode", [0, 1, 2, 3])
def test_a_pdm_port_keeps_its_bit_rate_as_native_rate_in_every_flow_mode(flow_mode):
    store = _pdm_store(flow_mode)
    assert store.native_rate(0, 0) == pytest.approx(_BIT_RATE)
    assert store.rate(0, 0) < _BIT_RATE / 8          # the playback rate is the decimated one


def test_a_normal_port_reports_the_rate_the_core_supplied():
    store = _pdm_store(0, core_rate=1_000_000.0, jitter=True)
    assert store.native_rate(0, 0) == 1_000_000.0


@pytest.mark.parametrize("flow_mode", [1, 2, 3])
def test_a_flow_controlled_pdm_port_measures_its_bit_rate_on_the_bits_own_positions(flow_mode):
    """The rate is taken between transport gaps of a jittered schedule, not from the core's
    figure (deliberately wrong here) and not from the dropout."""
    store = _pdm_store(flow_mode, core_rate=1_000_000.0, jitter=True)
    assert store.native_rate(0, 0) == pytest.approx(_BIT_RATE, rel=1e-3)
    assert store.rate(0, 0) < _BIT_RATE / 8


# ---- register-map JSON ----

def _load(doc):
    with import_notes.collect() as notes:
        pmap = PeripheralRegisterMap.from_json_dict(doc, "m.json")
    return pmap, [n.message for n in notes]


def test_a_field_with_inverted_or_out_of_byte_bits_is_dropped_and_reported():
    pmap, notes = _load({"registers": [{"name": "R", "address": "0x40000010", "fields": [
        {"name": "A", "bits": "[0:3]"}, {"name": "B", "bits": "[99:64]"},
        {"name": "C", "bits": "[3:0]"}]}]})
    assert [f.name for f in pmap.registers[0].fields] == ["C"]
    assert pmap.registers[0].fields[0].extract(0xA5) == 0x5
    assert len(notes) == 1 and "2 fields" in notes[0] and "R.A" in notes[0]


def test_a_register_address_outside_32_bits_is_skipped_and_reported():
    pmap, notes = _load({"registers": [{"name": "Far", "address": "0x100000010"},
                                       {"name": "Neg", "address": -4},
                                       {"name": "Ok", "address": "0x10000010"}]})
    assert [r.name for r in pmap.registers] == ["Ok"]
    assert len(notes) == 1 and "2 registers" in notes[0] and "Far" in notes[0]


@pytest.mark.parametrize("doc", [
    {"registers": "abc"}, {"_meta": [1, 2]}, [1, 2], {"registers": [3, "x"]},
    {"registers": [{"name": "R", "address": 1, "fields": 5}]},
    {"registers": [{"name": "R", "address": 1, "fields": [7]}]},
    {"registers": [{"name": "R", "address": 1,
                    "fields": [{"name": "f", "bits": "[0]", "enum": [1]}]}]},
])
def test_a_wrong_shaped_register_map_document_is_read_with_a_note_not_an_error(doc):
    pmap, notes = _load(doc)
    assert isinstance(pmap, PeripheralRegisterMap)
    assert any("wrong type" in n for n in notes)


# ---- importers ----

_BITS = ('<register name="A" address="0x10000000"><bit position="0" name="x"/>'
         '<reset hex="{}"/></register>')


def test_the_reset_hex_attribute_is_hexadecimal_with_or_without_a_prefix():
    for text, want in (("10", 0x10), ("0x10", 0x10), ("A5", 0xA5)):
        pmap, report = import_text(_BITS.format(text), "t")
        assert pmap.registers[0].reset == want and report.warnings == []
    _pmap, report = import_text(_BITS.format("zz"), "t")
    assert any("unparseable <reset hex>" in w for w in report.warnings)


_FIELDS = ("<Registers><Register><Name>R</Name><Address>0x10000000</Address>"
           "<DefaultValue>{}</DefaultValue></Register></Registers>"
           "<Fields><Field><Name>F</Name><Address>R[7:0]</Address></Field></Fields>")


def test_a_decimal_default_wider_than_the_fields_widens_the_register():
    pmap, report = import_text(_FIELDS.format("300"), "t")
    assert [r.reset for r in pmap.registers] == [300 & 0xFF, 300 >> 8]
    assert report.warnings == []


def test_a_default_with_bits_past_64_is_reported_as_dropped():
    _pmap, report = import_text(_FIELDS.format("2" + "0" * 24), "t")
    assert any("above the register's 64-bit width" in w for w in report.warnings)


def test_an_all_unknown_default_is_not_reported_as_not_a_number():
    for text in ("0xXX", "XX", "0bXXXXXXXX"):
        _pmap, report = import_text(_FIELDS.format(text), "t")
        assert not any("not a number" in w for w in report.warnings), text
    _pmap, report = import_text(_FIELDS.format("0xZZ"), "t")
    assert any("not a number" in w for w in report.warnings)


def test_register_map_bytes_decode_by_bom_or_say_which_encoding_was_guessed(tmp_path):
    xml = _BITS.format("0x01").replace('name="A"', 'name="Gain µs"')
    assert decode_text(xml.encode("utf-8")) == (xml, [])
    assert decode_text(xml.encode("utf-16")) == (xml, [])
    text, notes = decode_text(xml.encode("cp1252"))
    assert text == xml and notes == ["the file is not UTF-8: read as cp1252"]
    path = tmp_path / "amp16.xml"
    path.write_bytes(xml.encode("utf-16"))
    pmap, report = import_file(str(path))
    assert pmap.registers[0].name == "Gain µs" and report.warnings == []
    path.write_bytes(xml.encode("cp1252"))
    _pmap, report = import_file(str(path))
    assert report.warnings == ["the file is not UTF-8: read as cp1252"]


def test_the_json_round_trip_still_reads_a_clean_map_without_notes():
    doc = {"_meta": {"name": "amp"}, "registers": [{"name": "R", "address": "0x10000000",
           "fields": [{"name": "f", "bits": "[7:4]"}]}]}
    pmap, notes = _load(json.loads(json.dumps(doc)))
    assert notes == [] and pmap.registers[0].fields[0].hi == 7


# ---- store package ----

def test_the_audio_store_imports_without_loading_the_command_table_library():
    code = ("import sys, swi3s_studio.store.audio_store as a\n"
            "assert 'pyarrow' not in sys.modules\n"
            "from swi3s_studio.store import SCHEMA, load_commands\n"
            "assert 'pyarrow' in sys.modules\n")
    root = Path(__file__).resolve().parent.parent
    path = os.pathsep.join(p for p in (str(root), os.environ.get("PYTHONPATH", "")) if p)
    subprocess.run([sys.executable, "-c", code], check=True, cwd=root,
                   env={**os.environ, "PYTHONPATH": path}, timeout=120)


# ---- command store ----

def test_register_writes_yields_only_crc_valid_addressed_writes():
    from swi3s_studio.store.command_store import register_writes
    good = {"command": "WriteA32", "crc_valid": True, "has_address": True, "start_sample": 5,
            "address": 0x1081, "data": b"\x03", "device_mask": 1}
    cmds = [dict(good, crc_valid=False, address=0x1082), good,
            {"command": "SSCR", "is_commit": True, "crc_valid": True, "has_address": False,
             "start_sample": 9, "device_mask": 1},
            dict(good, has_address=False, address=0x1083),
            dict(good, command="ReadA32", address=0x1084)]
    assert list(register_writes(cmds)) == [(5, 0x1081, b"\x03", 1)]
