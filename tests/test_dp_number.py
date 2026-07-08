"""Per-DP logical data-port number (v3.0.0) — model, CSV, native, converter.

Covers Feature A: each of the 12 authoring slots carries a logical DP number
(0-31, default = slot index) plus a display name; (device, dp_number) must be
unique within a device; the number drives the DP register block address; and
the v1.74 converter emits the new DataPortNumber row.

Run: python3 tests/test_dp_number.py
"""
import importlib.util
import os
import tempfile

import swi3score
from swi3s_studio.model.bus_config import BusConfig, demo_config, CSV_APP_VERSION
from swi3s_studio.swviz.models import Interface
from swi3s_studio.swviz.viz import VizConfig
from swi3s_studio.swviz.io.csv_handler import CSVHandler

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _tmp(name="t.csv"):
    return os.path.join(tempfile.mkdtemp(), name)


def test_number_resolution():
    cfg = BusConfig()
    # Unset sentinel resolves to the slot index.
    assert cfg.dataports[5].dp_number == -1
    assert cfg.dataports[5].number(5) == 5
    # Explicit number overrides.
    cfg.dataports[5].dp_number = 20
    assert cfg.dataports[5].number(5) == 20


def test_uniqueness_within_device():
    cfg = demo_config()
    cfg.dataports[0].device_number = 0
    cfg.dataports[0].dp_number = 17
    cfg.dataports[1].device_number = 0
    cfg.dataports[1].dp_number = 17
    # BOTH slots of a colliding group are reported, so the authoring check catches
    # the clash no matter which slot the user just edited (not only the later one).
    assert set(cfg.duplicate_dp_numbers()) == {0, 1}
    cfg.dataports[1].device_number = 1
    assert cfg.duplicate_dp_numbers() == []          # same number, different device -> ok
    # Defaults (distinct = slot index) never collide.
    assert BusConfig().duplicate_dp_numbers() == []


def test_config_dataports_exports_only_enabled():
    # config_dataports() must not pad the export with disabled placeholder DPs
    # (BuildConfig used to prepend kMaxPeripherals empties). Every exported dataport
    # is an enabled one snooped from the bus.
    from swi3s_studio.session import Session
    sess = Session.from_demo(64, cold_start=False)
    d = sess.decoder.config_dataports()
    dps = d.get("dataports", [])
    assert dps, "demo should decode at least one dataport"
    assert all(e["enabled"] for e in dps), \
        f"config_dataports leaked disabled placeholder(s): {[e['enabled'] for e in dps]}"


def test_studio_csv_roundtrip_and_stamp():
    cfg = demo_config()
    cfg.dataports[0].dp_number = 17
    cfg.dataports[0].name = "MicL"
    p = _tmp()
    text = cfg.to_csv()
    assert f"AppVersion,{CSV_APP_VERSION}" in text
    assert CSV_APP_VERSION == "3.0.0"
    assert "DataPortNumber,17,1,2,3" in text         # resolved numbers per column
    cfg.to_csv_file(p)
    back = BusConfig.from_csv(p)
    assert back.dataports[0].dp_number == 17
    assert back.dataports[0].name == "MicL"
    assert back.dataports[1].dp_number == 1          # default resolved on write


def test_v2_backcompat_missing_row():
    # A pre-v3 CSV (no DataPortNumber row) must still load; number falls back to index.
    cfg = demo_config()
    text = cfg.to_csv()
    stripped = "\n".join(l for l in text.splitlines() if not l.startswith("DataPortNumber"))
    p = _tmp()
    with open(p, "w", encoding="utf-8") as f:
        f.write(stripped)
    back = BusConfig.from_csv(p)
    assert back.dataports[7].dp_number == -1         # absent -> sentinel
    assert back.dataports[7].number(7) == 7          # resolves to slot index


def test_swviz_csv_roundtrip():
    iface, viz = Interface(), VizConfig()
    viz.data_ports[4].dp_number = 9
    p = _tmp()
    CSVHandler.save_csv(p, iface, viz)
    iface2, viz2 = Interface(), VizConfig()
    res = CSVHandler.load_csv(p, iface2, viz2)
    assert res.success, res.error_message
    assert viz2.data_ports[4].dp_number == 9
    # An unset port resolves to its column index on write.
    assert viz2.data_ports[2].dp_number == 2


def test_cpp_register_addressing_follows_number():
    cfg = demo_config()
    cfg.dataports[0].dp_number = 17                  # re-target dev0 DP0 -> block 0x2000+17*0x100
    p = _tmp()
    cfg.to_csv_file(p)
    regs = swi3score.registers_from_csv(p)           # (device, address, value)
    block = 0x2000 + 17 * 0x100
    hits = [a for (d, a, _v) in regs if d == 0 and block <= a < block + 0x100]
    assert hits, f"no dev0 registers in DP17 block 0x{block:X}"


def test_converter_emits_dataport_number():
    spec = importlib.util.spec_from_file_location(
        "convert_174", os.path.join(_ROOT, "tools", "csv_converter", "convert_174.py"))
    conv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(conv)

    # Minimal v1.74 input (excess-one form), one enabled stereo source on DP0.
    old = {
        "Columns per Row": ["24"],
        "Skipping Denominator": ["16"],
        "Row Rate [kHz]": ["3072"],
        "Rows to Draw": ["64"],
        "Save file using excess one": ["True"],
        "Data Port Name": ["MicL"] + [f"DP{i}" for i in range(1, 12)],
        "Data Port Device Number": ["0"] * 12,
        "Data Port Channels": ["1"] + ["0"] * 11,
        "Data Port Enabled": ["True"] + ["False"] * 11,
        "Source": ["True"] * 12,
    }
    warnings = []
    iface, viz = conv.convert(old, warnings.append, source_stem="fixture")
    out = _tmp()
    CSVHandler.save_csv(out, iface, viz)
    with open(out, encoding="utf-8") as f:
        text = f.read()
    assert "AppVersion,3.0.0" in text
    # v1.74 has no DP-number concept -> defaults to the column index.
    assert "DataPortNumber,0,1,2,3,4,5,6,7,8,9,10,11" in text
    assert viz.data_ports[0].name == "MicL"
    # The converted file loads through the studio reader.
    assert BusConfig.from_csv(out).dataports[0].name == "MicL"


if __name__ == "__main__":
    test_number_resolution(); print("ok: number() resolves sentinel -> slot index")
    test_uniqueness_within_device(); print("ok: (device, dp_number) uniqueness")
    test_config_dataports_exports_only_enabled(); print("ok: config_dataports exports only enabled DPs")
    test_studio_csv_roundtrip_and_stamp(); print("ok: studio CSV v3.0.0 round-trip + stamp")
    test_v2_backcompat_missing_row(); print("ok: pre-v3 CSV (no row) back-compat")
    test_swviz_csv_roundtrip(); print("ok: swviz CSV round-trip")
    test_cpp_register_addressing_follows_number(); print("ok: C++ register block follows dp_number")
    test_converter_emits_dataport_number(); print("ok: v1.74 converter emits DataPortNumber")
    print("ALL DP-NUMBER TESTS PASSED")
