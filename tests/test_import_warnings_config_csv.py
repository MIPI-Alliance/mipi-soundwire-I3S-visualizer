"""Import warnings from a Visualizer config CSV: the native loader (swi3score.csv_warnings),
the format upgrade (ingest.visualizer_csv), the Python parsers (BusConfig.from_csv,
CSVHandler via viz_engine and the CLI), a Reserved CDS_TailWidth 3 and a Reserved per-port
value (TailWidth, BitWidth, PortMode, FCP_BitWidth, FCP_TailWidth) on each of those paths,
the legacy CDS guard, polarity and tail width carried to every source, Session's config
imports, and the window.

Every input is synthetic: a config the app writes (demo_config) with cells edited."""
import csv
import dataclasses
import io
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
import swi3score
from conftest import pump_loads

from swi3s_studio import import_notes
from swi3s_studio.ingest import visualizer_csv
from swi3s_studio.model.bus_config import BusConfig, demo_config
from swi3s_studio.session import Session

pytestmark = pytest.mark.gui

HERE = os.path.dirname(os.path.abspath(__file__))


def _rows():
    return list(csv.reader(io.StringIO(demo_config().to_csv())))


def _set(rows, key, col, value):
    """Set value column `col` (1 = DP0 / the scalar value) of row `key`."""
    for r in rows:
        if r and r[0] == key:
            r[col] = value
            return
    raise KeyError(key)


def _write(path, rows):
    path.write_text("\n".join(",".join(r) for r in rows) + "\n", encoding="utf-8")
    return str(path)


def _messages(fn, *a, **k):
    with import_notes.collect() as notes:
        out = fn(*a, **k)
    return out, notes


# ---------------------------------------------------------------- native loader

def test_a_config_the_app_writes_has_no_native_warnings(tmp_path):
    path = demo_config().to_csv_file(str(tmp_path / "clean.csv"))
    assert swi3score.csv_warnings(path) == []


def test_the_native_loader_names_the_rows_a_file_lacks(tmp_path):
    rows = [r for r in _rows()
            if r[0] not in ("CDS_DriveType_REG_PerSource", "CDS_EndDriveEarly_REG_PerSource")]
    assert swi3score.csv_warnings(_write(tmp_path / "lacking.csv", rows)) == [
        "2 rows are missing and take their defaults: CDS_DriveType_REG_PerSource, "
        "CDS_EndDriveEarly_REG_PerSource."]


def test_a_port_with_no_device_on_the_bus_is_reported_disabled(tmp_path):
    rows = _rows()
    _set(rows, "DeviceNumber_REG", 1, "12")              # DP0 is enabled with channels
    w = swi3score.csv_warnings(_write(tmp_path / "dev.csv", rows))
    assert w == ["DeviceNumber_REG names no device on the bus (0..11) for DP0 (12); that port "
                 "is disabled and left out of the audio, the grid and the registers."]
    assert swi3score.registers_from_csv(str(tmp_path / "dev.csv")) == []


def test_a_file_with_no_device_number_row_reads_its_ports_as_device_0(tmp_path):
    """DeviceNumber_REG missing: every port takes Device 0 in the native loader, as
    BusConfig reads it, so the enabled DP0 decodes."""
    rows = [r for r in _rows() if not (r and r[0] == "DeviceNumber_REG")]
    path = _write(tmp_path / "nodev.csv", rows)
    assert swi3score.csv_warnings(path) == [
        "1 row is missing and takes its default: DeviceNumber_REG."]
    with import_notes.collect():
        cfg = BusConfig.from_csv(path)
    assert {d.device_number for d in cfg.dataports} == {0}
    assert {(c["device"], c["dp"]) for c in swi3score.grid_from_csv(path, 8)
            if c["dp"] >= 0} == {(0, 0)}
    assert {dev for dev, _a, _v in swi3score.registers_from_csv(path)} == {0}


def test_a_manager_port_or_a_disabled_port_is_not_reported(tmp_path):
    rows = _rows()
    _set(rows, "ManagerDataport", 1, "True")             # DP0 becomes a manager port
    _set(rows, "DeviceNumber_REG", 3, "40")              # DP2 is disabled anyway
    assert swi3score.csv_warnings(_write(tmp_path / "mgr.csv", rows)) == []


def test_values_that_do_not_fit_their_register_field_are_reported(tmp_path):
    rows = _rows()
    _set(rows, "SampleSize_REG", 1, "40")
    _set(rows, "Interval_REG", 1, "5000")
    _set(rows, "NumColumns_REG", 1, "40")
    w = swi3score.csv_warnings(_write(tmp_path / "wide.csv", rows))
    assert ("NumColumns_REG 40 is outside 1..31 (2 to 32 columns); the decode uses 32 columns "
            "and the expected registers carry its low 5 bits (8).") in w
    assert ("SampleSize_REG does not fit its 5-bit register field (0..31) for DP0 = 40 (read "
            "as 8); the audio decode, the grid and the registers read its low 5 bits.") in w
    assert ("Interval_REG does not fit its 12-bit register field (0..4095) for DP0 = 5000; the "
            "expected registers carry its low 12 bits and the decode uses the value as "
            "written.") in w
    assert len(w) == 3


def test_a_column_count_at_the_int_limit_is_clamped_to_the_largest_count(tmp_path):
    rows = _rows()
    _set(rows, "NumColumns_REG", 1, "2147483647")
    assert swi3score.csv_warnings(_write(tmp_path / "cols.csv", rows)) == [
        "NumColumns_REG 2147483647 is outside 1..31 (2 to 32 columns); the decode uses 32 "
        "columns and the expected registers carry its low 5 bits (31)."]


def test_unreadable_cells_and_cells_past_the_last_column_are_reported_per_field(tmp_path):
    rows = _rows()
    _set(rows, "EnableCh_REG", 2, "abc")
    _set(rows, "EnableCh_REG", 3, "x")
    _set(rows, "Offset_REG", 3, "12abc")
    _set(rows, "PHY3Enabled", 1, "maybe")
    next(r for r in rows if r[0] == "Spacing_REG").append("9")
    w = swi3score.csv_warnings(_write(tmp_path / "cells.csv", rows))
    assert w == [
        "PHY3Enabled: 1 cell is not a valid value: 'maybe' read as false.",
        "EnableCh_REG: 2 cells are not a valid value: DP1 'abc' read as 0, DP2 'x' read as 0.",
        "Offset_REG: 1 cell is not a valid value: DP2 '12abc' read as 12.",
        "Spacing_REG: values past the last data port column (12) are ignored: '9'.",
    ]


def test_hex_and_binary_cells_read_as_their_values_and_bad_digits_are_reported(tmp_path):
    rows = _rows()
    _set(rows, "Interval_REG", 1, "0x7")                 # the demo's own values, re-spelt
    _set(rows, "EnableCh_REG", 1, "0b11")
    _set(rows, "HorizontalStart_REG", 1, "0X2")
    _set(rows, "SampleSize_REG", 1, "0B11")
    path = _write(tmp_path / "radix.csv", rows)
    assert swi3score.csv_warnings(path) == []
    clean = demo_config().to_csv_file(str(tmp_path / "clean.csv"))
    assert swi3score.registers_from_csv(path) == swi3score.registers_from_csv(clean)

    _set(rows, "Interval_REG", 1, "0xZZ")
    _set(rows, "Offset_REG", 1, "0b12")
    assert swi3score.csv_warnings(_write(tmp_path / "digits.csv", rows)) == [
        "Interval_REG: 1 cell is not a valid value: DP0 '0xZZ' read as 0.",
        "Offset_REG: 1 cell is not a valid value: DP0 '0b12' read as 1.",
    ]


def test_a_row_rate_with_trailing_text_is_reported_with_the_rate_read(tmp_path):
    rows = _rows()
    _set(rows, "RowRate", 1, "3072kHz")
    assert swi3score.csv_warnings(_write(tmp_path / "rate.csv", rows)) == [
        "RowRate: 1 cell is not a valid value: '3072kHz' read as 3072.0."]


def test_interface_and_per_source_values_that_do_not_fit_are_reported(tmp_path):
    rows = _rows()
    _set(rows, "SkippingDenominator_REG", 1, "5000")
    _set(rows, "CDS_BitWidth_REG", 1, "9")
    _set(rows, "CDS_TailWidth_REG_PerSource", 1, "5")
    _set(rows, "CDS_TailWidth_REG_PerSource", 3, "-1")
    _set(rows, "CDS_DriveType_REG_PerSource", 2, "2")
    assert swi3score.csv_warnings(_write(tmp_path / "fit.csv", rows)) == [
        "SkippingDenominator_REG 5000 does not fit its 12-bit register field (0..4095); the "
        "expected registers carry its low 12 bits (904) and the decode uses the value as "
        "written.",
        "CDS_BitWidth_REG 9 does not fit its 3-bit register field (0..7); the expected "
        "registers carry its low 3 bits (1) and the decode uses the value as written.",
        "CDS_TailWidth_REG_PerSource does not fit its 2-bit register field (0..3) for Manager = 5, "
        "Device 1 = -1; the expected registers carry its low 2 bits.",
        "CDS_DriveType_REG_PerSource does not fit its 1-bit register field (0..1) for Device 0 = 2; "
        "the expected registers carry its low bit.",
    ]


@pytest.mark.parametrize("key,value,message", [
    ("EnableCh_REG", "0x10003",
     "EnableCh_REG does not fit its 16 channel bits for DP0 = 65539; only channels 0..15 are "
     "read."),
    ("DataPortNumber", "40",
     "DataPortNumber is outside 0..31 for DP0 = 40; a negative number is ignored (the port "
     "keeps its column's number) and a larger one addresses no data port register block."),
    ("DataPortNumber", "-1",
     "DataPortNumber is outside 0..31 for DP0 = -1; a negative number is ignored (the port "
     "keeps its column's number) and a larger one addresses no data port register block."),
])
def test_a_channel_mask_or_port_number_out_of_range_is_reported(tmp_path, key, value, message):
    rows = _rows()
    _set(rows, key, 1, value)
    assert swi3score.csv_warnings(_write(tmp_path / "range.csv", rows)) == [message]


def test_an_unopenable_path_reports_why_and_the_core_is_unchanged(tmp_path):
    missing = str(tmp_path / "missing.csv")
    assert swi3score.csv_warnings(missing) == [f"Cannot open config CSV: {missing}"]
    from swi3s_studio.session import _REQUIRED_SCORE_ABI
    assert swi3score.score_abi == _REQUIRED_SCORE_ABI


# ---------------------------------------------------------------- the format upgrade

def test_a_current_file_upgrades_with_no_warnings(tmp_path):
    path = demo_config().to_csv_file(str(tmp_path / "cur.csv"))
    out, notes = _messages(visualizer_csv.normalized_path, path)
    assert out == path and notes == []


def test_a_file_with_no_config_field_says_every_setting_defaults(tmp_path):
    p = tmp_path / "capture.csv"
    p.write_text("Time [s],Channel 0,Channel 1\n0.0,0,1\n", encoding="utf-8")
    _out, notes = _messages(visualizer_csv.normalized_path, str(p))
    assert [str(n) for n in notes] == [
        "capture.csv: no config field is recognised (no _REG row and no legacy field); every "
        "setting takes its default."]


@pytest.mark.parametrize("text", ["", "  \n", "AppVersion,3.1.0\nDescription,x\n"])
def test_bus_config_says_every_setting_defaults_for_text_with_no_config_field(text):
    with import_notes.collect() as notes:
        BusConfig.from_csv_text(text, "bare.csv")
    assert [str(n) for n in notes] == [
        "bare.csv: no config field is recognised (no _REG row and no legacy field); every "
        "setting takes its default."]


def test_a_legacy_file_reports_skipped_fields_and_short_rows(tmp_path):
    p = tmp_path / "old.csv"
    p.write_text("Columns per Row (2-32),8\nRow Rate [kHz],3072\nFrobnicate,1\n"
                 "Data Port Enabled,True,False\nData Port Channels,2,1\n", encoding="utf-8")
    out, notes = _messages(visualizer_csv.normalized_path, str(p))
    try:
        assert [n.source for n in notes] == ["old.csv"] * 3
        assert notes[0].message == "1 legacy field is not recognised and was skipped: Frobnicate."
        assert notes[1].message == (
            "legacy rows missing, each taking its default for every data port or the "
            "interface: Data Port Device Number, Data Port Sample Width, Data Port Interval "
            "Integer, Data Port Offset, Data Port Horizontal Start, Data Port Horizontal "
            "Count, Source.")
        assert notes[2].message == (
            "legacy per-port rows with fewer values than data ports, the missing ports taking "
            "defaults: Data Port Enabled (2 of 12), Data Port Channels (2 of 12).")
    finally:
        if out != str(p):
            os.remove(out)


def test_a_v2_file_that_does_not_upgrade_says_it_is_read_as_written(tmp_path):
    rows = [r for r in _rows() if r[0] not in ("DataPortNumber", "FlowMode_REG")]
    path = _write(tmp_path / "v2.csv", rows)
    out, notes = _messages(visualizer_csv.normalized_path, path)
    assert out == path
    assert len(notes) == 1 and notes[0].source == "v2.csv"
    assert notes[0].message.startswith("this v2.0 config could not be upgraded to the current "
                                       "format (")
    assert notes[0].message.endswith("it is read as written, and fields v2.0 does not have "
                                     "take their defaults.")


def test_a_v2_field_the_upgrade_drops_is_reported(tmp_path):
    rows = [r for r in _rows() if r[0] != "DataPortNumber"] + [["Frobnicate", "1"]]
    out, notes = _messages(visualizer_csv.normalized_path, _write(tmp_path / "v2x.csv", rows))
    try:
        assert [n.message for n in notes] == [
            "upgrading this v2.0 config: 1 field is not recognised and was ignored: "
            f"Frobnicate (line {len(rows)})."]
    finally:
        os.remove(out)


def test_a_v2_upgrade_reports_the_fcp_registers_it_clears(tmp_path):
    """A port in FlowMode 0 or 1 does not use its flow control port, so the upgraded text
    writes its FCP registers as 0; each non-zero value cleared is an import note."""
    rows = [r for r in _rows() if r[0] != "DataPortNumber"]
    _set(rows, "FlowMode_REG", 1, "0")
    _set(rows, "FCP_HorizontalStart_REG", 1, "5")
    _set(rows, "FCP_GuardEnable_REG", 1, "True")
    _set(rows, "FlowMode_REG", 2, "1")
    _set(rows, "FCP_Offset_REG", 2, "3")
    _set(rows, "FlowMode_REG", 3, "2")              # Rx-Controlled keeps its FCP
    _set(rows, "FCP_HorizontalStart_REG", 3, "4")
    out, notes = _messages(visualizer_csv.normalized_path, _write(tmp_path / "v2f.csv", rows))
    try:
        assert [n.message for n in notes] == [
            "upgrading this v2.0 config: a flow control port is not in use in FlowMode 0 or 1, "
            "so its non-zero FCP registers are written as 0: DP0 (FCP_HorizontalStart_REG=5, "
            "FCP_GuardEnable_REG=True); DP1 (FCP_Offset_REG=3)."]
        with open(out, encoding="utf-8", newline="") as f:
            upgraded = {r[0]: r[1:] for r in csv.reader(f) if r}
        assert upgraded["FCP_HorizontalStart_REG"][:3] == ["0", "0", "4"]
    finally:
        os.remove(out)


# ---------------------------------------------------------------- the Python parsers

def test_bus_config_reports_cells_rows_and_fields_it_does_not_read(tmp_path):
    rows = _rows()
    _set(rows, "Interval_REG", 2, "seven")
    _set(rows, "GuardEnable_REG", 1, "Ture")
    next(r for r in rows if r[0] == "Spacing_REG").append("9")
    short = next(r for r in rows if r[0] == "HorizontalStart_REG")
    del short[5:]
    rows.append(["Frobnicate", "1"])
    cfg, notes = _messages(BusConfig.from_csv, _write(tmp_path / "t.tmp.csv", rows),
                           source="/somewhere/orig.csv")
    assert {n.source for n in notes} == {"orig.csv"}
    assert [n.message for n in notes] == [
        "Interval_REG: 1 cell is not a valid value: DP1 'seven' read as 0.",
        "GuardEnable_REG: 1 cell is not a valid value: DP0 'Ture' read as false.",
        "rows with fewer values than data ports or CDS sources, the missing cells reading as "
        "their default: HorizontalStart_REG (4 of 12).",
        "values past the last data port or CDS source column are ignored: Spacing_REG ('9').",
        f"1 field is not recognised and was ignored: Frobnicate (line {len(rows)}).",
    ]
    assert cfg.dataports[1].interval == 0


def test_bus_config_reads_a_config_it_wrote_with_no_warnings(tmp_path):
    path = demo_config().to_csv_file(str(tmp_path / "clean.csv"))
    _cfg, notes = _messages(BusConfig.from_csv, path)
    assert notes == []


def test_the_csv_handler_reports_unrecognised_and_defaulted_fields_but_not_description(tmp_path):
    from swi3s_studio.model.viz_engine import build_bus_model
    clean = demo_config().to_csv_file(str(tmp_path / "clean.csv"))   # writes no Description
    _m, notes = _messages(build_bus_model, clean, 4)
    assert notes == []
    rows = [r for r in _rows() if r[0] != "RowRate"] + [["Frobnicate", "1"]]
    _m, notes = _messages(build_bus_model, _write(tmp_path / "odd.csv", rows), 4)
    assert [str(n) for n in notes] == [
        f"odd.csv: 1 field is not recognised and was ignored: Frobnicate (line {len(rows)}).",
        "odd.csv: 1 field is missing and takes its default: Interface: RowRate."]


@pytest.mark.parametrize("number", ["-1", "-2", "12"])
def test_the_csv_handler_disables_a_port_whose_device_number_names_no_device(tmp_path, number):
    """Only the ManagerDataport flag makes a Manager port. A DeviceNumber_REG outside 0..11
    without it, -1 included, disables the port and is reported in the native loader's words,
    and the port draws nothing."""
    from swi3s_studio.model.viz_engine import build_bus_model
    from swi3s_studio.swviz.config.constants import SpecialDevices
    rows = _rows()
    _set(rows, "DeviceNumber_REG", 1, number)            # DP0 is enabled with channels
    path = _write(tmp_path / "dev.csv", rows)
    want = (f"DeviceNumber_REG names no device on the bus (0..11) for DP0 ({number}); that "
            "port is disabled and left out of the audio, the grid and the registers.")
    assert swi3score.csv_warnings(path) == [want]
    (bm, iface, viz), notes = _messages(build_bus_model, path, 4)
    assert [n.message for n in notes] == [want]
    assert viz.data_ports[0].enabled is False
    assert iface.get_dp_device(0) not in (SpecialDevices.MANAGER, SpecialDevices.UNIVERSAL)
    assert not [b for b in bm.bits if b.dp == 0]


@pytest.mark.parametrize("number", ["40", "32", "-5", "-1"])
def test_the_csv_handler_reports_a_data_port_number_outside_0_31(tmp_path, number):
    """A DataPortNumber outside 0..31 on a drawn port with a channel is kept and reported in
    the native loader's words; the port is still drawn."""
    from swi3s_studio.model.viz_engine import build_bus_model
    rows = _rows()
    _set(rows, "DataPortNumber", 1, number)              # DP0 is enabled with channels
    path = _write(tmp_path / "dpn.csv", rows)
    want = (f"DataPortNumber is outside 0..31 for DP0 = {number}; a negative number is ignored "
            "(the port keeps its column's number) and a larger one addresses no data port "
            "register block.")
    assert swi3score.csv_warnings(path) == [want]
    (bm, _iface, viz), notes = _messages(build_bus_model, path, 4)
    assert [n.message for n in notes] == [want]
    assert viz.data_ports[0].dp_number == int(number)
    assert [b for b in bm.bits if b.dp == 0]


@pytest.mark.parametrize("number", ["0", "31"])
def test_the_csv_handler_reads_a_data_port_number_in_0_31_silently(tmp_path, number):
    from swi3s_studio.model.viz_engine import build_bus_model
    rows = _rows()
    _set(rows, "DataPortNumber", 1, number)
    _m, notes = _messages(build_bus_model, _write(tmp_path / "dpn.csv", rows), 4)
    assert notes == []


def test_a_v2_upgrade_keeps_a_bare_minus_one_off_the_manager(tmp_path):
    """The v2.0 upgrade writes a port whose bare DeviceNumber_REG is -1 disabled, not as a
    Manager port, so the native decode leaves it out as it does the original file."""
    rows = [r for r in _rows() if r[0] != "DataPortNumber"]
    _set(rows, "DeviceNumber_REG", 1, "-1")
    out, notes = _messages(visualizer_csv.normalized_path, _write(tmp_path / "v2m.csv", rows))
    try:
        assert [n.message for n in notes] == [
            "upgrading this v2.0 config: DeviceNumber_REG names no device on the bus (0..11) "
            "for DP0 (-1); that port is disabled and left out of the audio, the grid and the "
            "registers."]
        with open(out, encoding="utf-8", newline="") as f:
            upgraded = {r[0]: r[1:] for r in csv.reader(f) if r}
        assert upgraded["ManagerDataport"][0] == "False"
        assert upgraded["Enabled"][0] == "False"
    finally:
        os.remove(out)


def test_the_cli_prints_the_warnings(tmp_path, capsys):
    from swi3s_studio import cli
    rows = _rows() + [["Frobnicate", "1"]]
    code = cli.run_headless(_write(tmp_path / "odd.csv", rows), str(tmp_path / "m.json"))
    assert code == 0
    err = capsys.readouterr().err
    assert ("warning: odd.csv: 1 field is not recognised and was ignored: Frobnicate "
            f"(line {len(rows)}).") in err


def test_bus_config_reports_a_field_given_on_more_than_one_row_as_the_native_loader_does(tmp_path):
    """A repeated field, an interface row or a data-port row, is reported once in the native
    loader's words and the last row is used; a repeated AppVersion is not reported."""
    rows = _rows()
    interval = next(r for r in rows if r and r[0] == "Interval_REG")
    later = ["Interval_REG"] + [str(int(c) + 1) for c in interval[1:]]
    rate = next(r for r in rows if r and r[0] == "RowRate")
    path = _write(tmp_path / "repeat.csv", [rows[0]] + rows + [later, list(rate), list(rate)])
    with import_notes.collect() as notes:
        cfg = BusConfig.from_csv(path)
    native = list(swi3score.csv_warnings(path))
    got = [n.message for n in notes]
    assert "Interval_REG appears 2 times; the last row is used." in got
    assert "RowRate appears 3 times; the last row is used." in got
    assert not [m for m in got if m.startswith("AppVersion")]
    assert [d.interval for d in cfg.dataports] == [int(c) + 1 for c in interval[1:]]
    assert {m for m in got if "appears" in m} == {m for m in native if "appears" in m}


# ---------------------------------------------------------------- Reserved CDS_TailWidth 3

RESERVED = "CDS_TailWidth 3 is Reserved (0..2 UI are defined) for "


def _tail_rows(cells):
    """The demo rows with the per-source tail row set to `cells` (source index -> text)."""
    rows = _rows()
    for index, text in cells.items():
        _set(rows, "CDS_TailWidth_REG_PerSource", index + 1, text)
    return rows


def _scalar_tail_rows(width):
    """The demo rows with the legacy scalar tail row in place of the per-source one."""
    return [r for r in _rows() if r[0] != "CDS_TailWidth_REG_PerSource"] + [
        ["CDS_TailWidth_REG", str(width)]]


def test_the_reserved_tail_width_is_the_same_value_in_both_engines():
    from swi3s_studio.model import bus_config
    from swi3s_studio.swviz.config.constants import InterfaceRanges
    assert bus_config.CDS_TAIL_RESERVED == InterfaceRanges.RESERVED_CDS_TAIL_WIDTH == 3


def test_bus_config_keeps_a_reserved_tail_width_and_reports_it_once(tmp_path):
    rows = _tail_rows({2: "3"})
    cfg, notes = _messages(BusConfig.from_csv, _write(tmp_path / "t.csv", rows),
                           source="/somewhere/orig.csv")
    assert cfg.cds_tail[2] == 3
    assert [(n.source, n.message) for n in notes] == [(
        "orig.csv",
        "CDS_TailWidth_REG_PerSource: " + RESERVED + "Device 1 = 3; the value is kept as written, "
        "the grid draws a 3-UI tail and the expected registers carry 3.")]


@pytest.mark.parametrize("width", ["0", "1", "2"])
def test_bus_config_reports_nothing_for_a_defined_tail_width(tmp_path, width):
    cfg, notes = _messages(BusConfig.from_csv,
                           _write(tmp_path / "t.csv", _tail_rows({0: width, 5: width})))
    assert cfg.cds_tail[0] == cfg.cds_tail[5] == int(width)
    assert notes == []


@pytest.mark.parametrize("width", ["4", "7"])
def test_a_tail_width_past_the_register_is_not_also_reported_as_reserved(tmp_path, width):
    _cfg, notes = _messages(BusConfig.from_csv,
                            _write(tmp_path / "t.csv", _tail_rows({1: width})))
    assert [n.message for n in notes] == [
        "CDS_TailWidth_REG_PerSource does not fit its 2-bit register field (0..3) for "
        f"Device 0 = {width}; the expected registers carry its low 2 bits."]


def test_bus_config_names_every_source_of_a_scalar_reserved_tail_width(tmp_path):
    cfg, notes = _messages(BusConfig.from_csv,
                           _write(tmp_path / "t.csv", _scalar_tail_rows(3)))
    assert cfg.cds_tail == [3] * 13
    reserved = [n.message for n in notes if RESERVED in n.message]
    assert len(reserved) == 1
    assert reserved[0].startswith("CDS_TailWidth_REG_PerSource: " + RESERVED + "Manager = 3, "
                                  "Device 0 = 3, Device 1 = 3 and 10 more;")
    _cfg, notes = _messages(BusConfig.from_csv, _write(tmp_path / "u.csv", _scalar_tail_rows(2)))
    assert not [n for n in notes if "Reserved" in n.message]


def test_the_csv_handler_keeps_a_reserved_tail_width_and_reports_it_once(tmp_path):
    from swi3s_studio.model.viz_engine import build_bus_model
    rows = _tail_rows({0: "3", 4: "3", 2: "1"})
    line = 1 + next(i for i, r in enumerate(rows) if r[0] == "CDS_TailWidth_REG_PerSource")
    _m, notes = _messages(build_bus_model, _write(tmp_path / "t.csv", rows), 4)
    assert [(n.source, n.message) for n in notes] == [(
        "t.csv", f"Line {line} (CDS_TailWidth_REG_PerSource): CDS_TailWidth 3 is Reserved (0..2 UI "
        "are defined) for Manager = 3; Device 3 = 3; the value is kept as written and the grid "
        "draws a 3-UI tail.")]
    (_m, iface, _viz), _notes = _messages(build_bus_model, _write(tmp_path / "u.csv", rows), 4)
    assert iface.CDS_Tail_PerSource[:5] == [3, 0, 1, 0, 3]
    assert iface.CDS_TailWidth_REG == 3


@pytest.mark.parametrize("width", ["0", "1", "2"])
def test_the_csv_handler_reports_nothing_for_a_defined_tail_width(tmp_path, width):
    from swi3s_studio.model.viz_engine import build_bus_model
    _m, notes = _messages(build_bus_model,
                          _write(tmp_path / "t.csv", _tail_rows({0: width, 3: width})), 4)
    assert notes == []


@pytest.mark.parametrize("width", ["4", "7"])
def test_the_csv_handler_does_not_report_a_wide_tail_width_as_reserved(tmp_path, width):
    from swi3s_studio.model.viz_engine import build_bus_model
    _m, notes = _messages(build_bus_model,
                          _write(tmp_path / "t.csv", _tail_rows({1: width})), 4)
    assert len(notes) == 1
    assert "does not fit its 2-bit register field" in notes[0].message
    assert "Reserved" not in notes[0].message


def test_the_csv_handler_reports_a_scalar_reserved_tail_width_for_every_source(tmp_path):
    from swi3s_studio.model.viz_engine import build_bus_model
    rows = _scalar_tail_rows(3)
    _m, notes = _messages(build_bus_model, _write(tmp_path / "t.csv", rows), 4)
    reserved = [n.message for n in notes if "Reserved" in n.message]
    assert len(reserved) == 1
    assert reserved[0].startswith(f"Line {len(rows)} (CDS_TailWidth_REG): " + RESERVED
                                  + "Manager = 3; Device 0 = 3;")
    assert reserved[0].endswith("Device 11 = 3; the value is kept as written and the grid "
                                "draws a 3-UI tail.")
    _m, notes = _messages(build_bus_model, _write(tmp_path / "u.csv", _scalar_tail_rows(2)), 4)
    assert not [n for n in notes if "Reserved" in n.message]


def test_the_engine_side_report_names_a_reserved_tail_width_as_bus_config_words_it(tmp_path):
    path = _write(tmp_path / "t.csv", _tail_rows({2: "3"}))
    with import_notes.collect() as notes:
        msgs = visualizer_csv.report_csv_warnings(path, "/somewhere/orig.csv")
    want = ("CDS_TailWidth_REG_PerSource: " + RESERVED + "Device 1 = 3; the value is kept as "
            "written, the grid draws a 3-UI tail and the expected registers carry 3.")
    assert msgs == [want]
    assert [(n.source, n.message) for n in notes] == [("orig.csv", want)]
    # One file read by BusConfig as well reports it once.
    with import_notes.collect() as both:
        visualizer_csv.report_csv_warnings(path, "/somewhere/orig.csv")
        BusConfig.from_csv(path, source="/somewhere/orig.csv")
    assert [n.message for n in both] == [want]


@pytest.mark.parametrize("width", ["0", "1", "2", "5"])
def test_the_engine_side_report_adds_no_reserved_note_for_another_width(tmp_path, width):
    path = _write(tmp_path / "t.csv", _tail_rows({2: width}))
    msgs, _notes = _messages(visualizer_csv.report_csv_warnings, path)
    assert not [m for m in msgs if "Reserved" in m]


@pytest.mark.parametrize("cells,listed", [
    ({2: "3"}, "Device 1 = 3"),
    ({0: "3", 4: "3", 2: "1"}, "Manager = 3, Device 3 = 3"),
    ({i: "3" for i in range(5)}, "Manager = 3, Device 0 = 3, Device 1 = 3 and 2 more"),
])
def test_the_native_loader_words_a_reserved_tail_width_as_bus_config_does(tmp_path, cells,
                                                                          listed):
    from swi3s_studio.model.bus_config import reserved_cds_tail_note
    path = _write(tmp_path / "t.csv", _tail_rows(cells))
    want = ("CDS_TailWidth_REG_PerSource: " + RESERVED + listed + "; the value is kept as written, "
            "the grid draws a 3-UI tail and the expected registers carry 3.")
    assert swi3score.csv_warnings(path) == [want]
    with import_notes.collect():
        cfg = BusConfig.from_csv(path)
    assert reserved_cds_tail_note(cfg.reserved_cds_tail_sources()) == want


def test_the_native_loader_names_every_source_of_a_scalar_reserved_tail_width(tmp_path):
    warnings = swi3score.csv_warnings(_write(tmp_path / "t.csv", _scalar_tail_rows(3)))
    assert [w for w in warnings if "Reserved" in w] == [
        "CDS_TailWidth_REG_PerSource: " + RESERVED + "Manager = 3, Device 0 = 3, Device 1 = 3 and 10 "
        "more; the value is kept as written, the grid draws a 3-UI tail and the expected "
        "registers carry 3."]
    assert not [w for w in swi3score.csv_warnings(_write(tmp_path / "u.csv", _scalar_tail_rows(2)))
                if "Reserved" in w]


@pytest.mark.parametrize("width", ["4", "7", "-1"])
def test_the_native_loader_reports_a_wide_tail_width_only_as_wide(tmp_path, width):
    assert swi3score.csv_warnings(_write(tmp_path / "t.csv", _tail_rows({1: width}))) == [
        "CDS_TailWidth_REG_PerSource does not fit its 2-bit register field (0..3) for "
        f"Device 0 = {width}; the expected registers carry its low 2 bits."]


def test_a_legacy_reserved_tail_width_read_by_the_native_route_is_reported_once(tmp_path):
    p = tmp_path / "old.csv"
    p.write_text("Columns per Row (2-32),8\nCDS Tail Width,3\n", encoding="utf-8")
    with import_notes.collect() as notes:
        out = visualizer_csv.normalized_path(str(p))
        try:
            visualizer_csv.report_csv_warnings(out, str(p))
            BusConfig.from_csv(out, source=str(p))
        finally:
            os.remove(out)
    reserved = [n for n in notes if "Reserved" in n.message]
    assert [(n.source, n.message) for n in reserved] == [(
        "old.csv", "CDS_TailWidth_REG_PerSource: " + RESERVED + "Manager = 3, Device 0 = 3, "
        "Device 1 = 3 and 10 more; the value is kept as written, the grid draws a 3-UI tail "
        "and the expected registers carry 3.")]


# ---------------------------------------------------------------- Reserved per-port values

# (attribute, CSV row, the note's field and reserved-value text, what the kept value does,
# the values defined).
_DP_RESERVED_CASES = [
    ("tail_width", "TailWidth_REG", "TailWidth 3 is Reserved (0..2 UI are defined)",
     "tails are 3 UI", (0, 1, 2)),
    ("bit_width", "BitWidth_REG", "BitWidth 3 is Reserved (0..2, 1 to 3 UI, are defined)",
     "data bits are 4 UI", (0, 1, 2)),
    ("port_mode", "PortMode_REG",
     "PortMode 1 is Reserved (0 Normal, 2 Test0 and 3 Test1 are defined)",
     "the port is drawn as Normal", (0, 2, 3)),
    ("fcp_bit_width", "FCP_BitWidth_REG",
     "FCP_BitWidth 3 is Reserved (0..2, 1 to 3 UI, are defined)", "DRQ bits are 4 UI",
     (0, 1, 2)),
    ("fcp_tail_width", "FCP_TailWidth_REG", "FCP_TailWidth 3 is Reserved (0..2 UI are defined)",
     "FCP tails are 3 UI", (0, 1, 2)),
]
_DP_RESERVED_IDS = [c[1] for c in _DP_RESERVED_CASES]
_RESERVED_VALUE = {"port_mode": 1}


def _reserved_value(attr):
    return _RESERVED_VALUE.get(attr, 3)


def _ports_config(attr, value, ports=(0,)):
    """The demo config with each of `ports` set up as DP0 is and its `attr` at `value`."""
    cfg = demo_config()
    for i in ports:
        cfg.dataports[i] = dataclasses.replace(cfg.dataports[0])
        setattr(cfg.dataports[i], attr, value)
    return cfg


def _dp_note(row, text, effect, listed):
    return (f"{row}: {text} for {listed}; the value is kept as written, {effect} and the "
            f"expected registers carry {text.split()[1]}.")


def test_the_reserved_port_values_are_the_same_in_every_engine():
    from swi3s_studio.model import bus_config
    from swi3s_studio.swviz.config.constants import DataPortRanges
    from swi3s_studio.swviz.io import csv_handler
    assert [(row, value) for _a, row, _n, value, _d, _e in bus_config.DP_RESERVED] == [
        (row, value) for row, (_n, value, _d, _e) in csv_handler._DP_RESERVED.items()] == [
        ("TailWidth_REG", DataPortRanges.RESERVED_TAIL_WIDTH),
        ("BitWidth_REG", DataPortRanges.RESERVED_BIT_WIDTH),
        ("PortMode_REG", DataPortRanges.RESERVED_PORT_MODE),
        ("FCP_BitWidth_REG", DataPortRanges.RESERVED_BIT_WIDTH),
        ("FCP_TailWidth_REG", DataPortRanges.RESERVED_TAIL_WIDTH)]
    assert [(a, r) for a, r, *_rest in bus_config.DP_RESERVED] == [
        (c[0], c[1]) for c in _DP_RESERVED_CASES]


@pytest.mark.parametrize("attr,row,text,effect,_defined", _DP_RESERVED_CASES,
                         ids=_DP_RESERVED_IDS)
def test_bus_config_keeps_a_reserved_port_value_and_reports_it_once(tmp_path, attr, row, text,
                                                                    effect, _defined):
    value = _reserved_value(attr)
    path = _ports_config(attr, value).to_csv_file(str(tmp_path / "t.csv"))
    cfg, notes = _messages(BusConfig.from_csv, path, source="/somewhere/orig.csv")
    assert getattr(cfg.dataports[0], attr) == value
    assert [(n.source, n.message) for n in notes] == [
        ("orig.csv", _dp_note(row, text, effect, f"DP0 = {value}"))]


@pytest.mark.parametrize("attr,row,text,effect,defined", _DP_RESERVED_CASES,
                         ids=_DP_RESERVED_IDS)
def test_no_reader_reports_a_defined_port_value(tmp_path, attr, row, text, effect, defined):
    from swi3s_studio.model.viz_engine import build_bus_model
    for value in defined:
        path = _ports_config(attr, value, (0, 3)).to_csv_file(str(tmp_path / f"{value}.csv"))
        _cfg, notes = _messages(BusConfig.from_csv, path)
        assert not [n for n in notes if "Reserved" in n.message], (value, notes)
        assert not [w for w in swi3score.csv_warnings(path) if "Reserved" in w], value
        _m, notes = _messages(build_bus_model, path, 4)
        assert not [n for n in notes if "Reserved" in n.message], (value, notes)


@pytest.mark.parametrize("attr,row,text,effect,_defined", _DP_RESERVED_CASES,
                         ids=_DP_RESERVED_IDS)
def test_a_port_value_past_the_register_is_not_also_reported_as_reserved(tmp_path, attr, row,
                                                                         text, effect,
                                                                         _defined):
    path = _ports_config(attr, 7).to_csv_file(str(tmp_path / "t.csv"))
    _cfg, notes = _messages(BusConfig.from_csv, path)
    assert [n.message for n in notes] == [
        f"{row} does not fit its 2-bit register field (0..3) for DP0 = 7; the expected "
        "registers carry its low 2 bits and the decode uses the value as written."]
    assert swi3score.csv_warnings(path) == [n.message for n in notes]


@pytest.mark.parametrize("attr,row,text,effect,_defined", _DP_RESERVED_CASES,
                         ids=_DP_RESERVED_IDS)
def test_a_disabled_port_s_reserved_value_is_reported_by_no_reader(tmp_path, attr, row, text,
                                                                   effect, _defined):
    from swi3s_studio.model.viz_engine import build_bus_model
    cfg = _ports_config(attr, _reserved_value(attr), (0, 4))
    cfg.dataports[4].enabled = False
    setattr(cfg.dataports[0], attr, 0)
    path = cfg.to_csv_file(str(tmp_path / "t.csv"))
    _cfg, notes = _messages(BusConfig.from_csv, path)
    assert notes == []
    assert swi3score.csv_warnings(path) == []
    _m, notes = _messages(build_bus_model, path, 4)
    assert not [n for n in notes if "Reserved" in n.message]


@pytest.mark.parametrize("attr,row,text,effect,_defined", _DP_RESERVED_CASES,
                         ids=_DP_RESERVED_IDS)
def test_the_native_loader_words_a_reserved_port_value_as_bus_config_does(tmp_path, attr, row,
                                                                          text, effect,
                                                                          _defined):
    from swi3s_studio.model.bus_config import reserved_dp_note
    value = _reserved_value(attr)
    path = _ports_config(attr, value, (0, 3, 5, 7, 9)).to_csv_file(str(tmp_path / "t.csv"))
    listed = f"DP0 = {value}, DP3 = {value}, DP5 = {value} and 2 more"
    want = _dp_note(row, text, effect, listed)
    assert [w for w in swi3score.csv_warnings(path) if "Reserved" in w] == [want]
    assert reserved_dp_note(row, [f"DP{i} = {value}" for i in (0, 3, 5, 7, 9)]) == want
    # One file read by both engines reports it once.
    with import_notes.collect() as both:
        visualizer_csv.report_csv_warnings(path, "/somewhere/orig.csv")
        BusConfig.from_csv(path, source="/somewhere/orig.csv")
    assert [(n.source, n.message) for n in both if "Reserved" in n.message] == [
        ("orig.csv", want)]


@pytest.mark.parametrize("attr,row,text,effect,_defined", _DP_RESERVED_CASES,
                         ids=_DP_RESERVED_IDS)
def test_the_csv_handler_keeps_a_reserved_port_value_and_reports_it_once(tmp_path, attr, row,
                                                                         text, effect,
                                                                         _defined):
    from swi3s_studio.model.viz_engine import build_bus_model
    value = _reserved_value(attr)
    path = _ports_config(attr, value, (0, 3)).to_csv_file(str(tmp_path / "t.csv"))
    with open(path, encoding="utf-8") as f:
        line = 1 + next(i for i, r in enumerate(csv.reader(f)) if r and r[0] == row)
    (_m, iface, _viz), notes = _messages(build_bus_model, path, 4)
    assert [(n.source, n.message) for n in notes if "Reserved" in n.message] == [(
        "t.csv", f"Line {line} ({row}): {text} for DP0 = {value}; DP3 = {value}; the value is "
        f"kept as written and {effect}.")]
    for i in (0, 3):
        held = iface.get_fcp(i).config if row.startswith("FCP_") else iface.data_ports[i].config
        assert getattr(held, row) == value


def test_the_csv_handler_names_the_row_it_uses_for_a_repeated_reserved_row(tmp_path):
    from swi3s_studio.model.viz_engine import build_bus_model
    rows = _rows()
    first = next(r for r in rows if r[0] == "TailWidth_REG")
    rows.append(["TailWidth_REG"] + ["3"] + first[2:])
    _m, notes = _messages(build_bus_model, _write(tmp_path / "t.csv", rows), 4)
    assert [n.message for n in notes if "Reserved" in n.message] == [
        f"Line {len(rows)} (TailWidth_REG): TailWidth 3 is Reserved (0..2 UI are defined) for "
        "DP0 = 3; the value is kept as written and tails are 3 UI."]


def test_a_legacy_tail_width_of_3_is_carried_to_every_source(tmp_path):
    p = tmp_path / "old.csv"
    p.write_text("Columns per Row (2-32),8\nCDS Tail Width,3\n", encoding="utf-8")
    out = visualizer_csv.normalized_path(str(p))
    try:
        cfg, notes = _messages(BusConfig.from_csv, out, source=str(p))
        assert cfg.cds_tail == [3] * 13
        assert [n for n in notes if "Reserved" in n.message] != []
        assert {n.source for n in notes} == {"old.csv"}
    finally:
        os.remove(out)


def _legacy_rows(path, extra):
    """The current-format rows {name: values} a legacy file of `extra` lines upgrades to,
    and the upgrade's import notes."""
    path.write_text("Columns per Row (2-32),8\nRow Rate [kHz],3072\n" + "".join(
        f"{line}\n" for line in extra), encoding="utf-8")
    with import_notes.collect() as notes:
        out = visualizer_csv.normalized_path(str(path))
    try:
        with open(out, encoding="utf-8") as f:
            rows = {r[0]: r[1:] for r in csv.reader(f) if r}
        with import_notes.collect():
            cfg = BusConfig.from_csv(out)
        native = swi3score.registers_from_csv(out)
    finally:
        os.remove(out)
    return rows, cfg, native, [n.message for n in notes]


@pytest.mark.parametrize("enabled,polarity,en,pol", [
    ("True", "False", True, 0), ("True", "True", True, 1), ("False", "False", False, 0),
    ("yes", "1", True, 1), ("False", "True", False, 1)])
def test_a_legacy_guard_and_polarity_are_carried_to_every_source(tmp_path, enabled, polarity,
                                                                 en, pol):
    """The enable and the polarity reach every source independently: a polarity with the
    guard off is kept, not reported."""
    rows, cfg, _native, notes = _legacy_rows(
        tmp_path / "old.csv", [f"CDS Guard Enabled,{enabled}", f"CDS Guard Polarity,{polarity}"])
    assert notes == []
    assert rows["CDS_GuardEnable_REG_PerSource"] == [str(int(en))] * 13
    assert rows["CDS_GuardPolarity_REG_PerSource"] == [str(pol)] * 13
    assert cfg.cds_guard_enable == [en] * 13 and cfg.cds_guard_polarity == [pol] * 13
    from swi3s_studio.model.viz_engine import build_bus_model
    p = tmp_path / "old.csv"
    out = visualizer_csv.normalized_path(str(p))
    try:
        (_m, iface, _viz), _n = _messages(build_bus_model, out, 4)
    finally:
        os.remove(out)
    assert iface.CDS_GuardEnable_PerSource == [en] * 13
    assert iface.CDS_GuardPolarity_PerSource == [pol] * 13


def test_a_legacy_guard_reaches_the_native_expected_registers(tmp_path):
    """The native engine's expected registers for a port's device carry the legacy guard:
    off, Guard 0 and Guard 1 differ in one register, the device's CDS guard."""
    port = ["Data Port Enabled,True", "Data Port Channels,1"]

    def registers(name, guard_rows):
        _rows, _cfg, native, _notes = _legacy_rows(tmp_path / name, port + guard_rows)
        return {(dev, addr): value for dev, addr, value in native}

    off = registers("off.csv", [])
    g0 = registers("g0.csv", ["CDS Guard Enabled,True", "CDS Guard Polarity,False"])
    g1 = registers("g1.csv", ["CDS Guard Enabled,True", "CDS Guard Polarity,True"])
    assert off.keys() == g0.keys() == g1.keys()
    changed = {k for k in off if not off[k] == g0[k] == g1[k]}
    assert len(changed) == 1
    [key] = changed
    assert len({off[key], g0[key], g1[key]}) == 3


def test_a_legacy_file_without_guard_rows_upgrades_as_before(tmp_path):
    """No guard row: every source is off, and adding the guard rows changes only the two
    per-source guard rows of the upgraded file."""
    plain, cfg, _native, notes = _legacy_rows(tmp_path / "plain.csv", ["CDS Tail Width,2"])
    assert notes == [] and cfg.cds_guard_enable == [False] * 13
    assert cfg.cds_guard_polarity == [0] * 13
    assert plain["CDS_GuardEnable_REG_PerSource"] == plain["CDS_GuardPolarity_REG_PerSource"] == ["0"] * 13
    guarded, _cfg, _native, _notes = _legacy_rows(
        tmp_path / "guarded.csv",
        ["CDS Tail Width,2", "CDS Guard Enabled,True", "CDS Guard Polarity,True"])
    changed = {k for k in plain.keys() | guarded.keys() if plain.get(k) != guarded.get(k)}
    assert changed == {"CDS_GuardEnable_REG_PerSource", "CDS_GuardPolarity_REG_PerSource"}


_CLAMPED = ("1 legacy value did not fit and was changed: CDS/S0 Handover Width {w} is "
            "outside 0 to 3; ")


@pytest.mark.parametrize("width,handover,tail,message", [
    (-1, False, 0, _CLAMPED + "an S1 tail width of 0 is used."),
    (0, False, 0, None),
    (3, True, 2, None),
    (4, True, 2, _CLAMPED + "a handover and an S1 tail width of 2 are used."),
    (8, True, 2, _CLAMPED + "a handover and an S1 tail width of 2 are used.")])
def test_a_legacy_handover_width_outside_0_to_3_is_reported(tmp_path, width, handover, tail,
                                                            message):
    rows, cfg, _native, notes = _legacy_rows(tmp_path / "old.csv",
                                             [f"CDS/S0 Handover Width,{width}"])
    assert notes == ([] if message is None else [message.format(w=width)])
    assert rows["EnforceCDSHandover"] == [str(handover)]
    assert rows["S1TailWidth_REG"] == [str(tail)]
    assert (cfg.enforce_cds_handover, cfg.s1_tail_width) == (handover, tail)


# ---------------------------------------------------------------- Session's config imports

def _bad_device_csv(tmp_path, name="dev.csv"):
    rows = _rows()
    _set(rows, "DeviceNumber_REG", 1, "12")
    return _write(tmp_path / name, rows)


def test_applying_a_csv_names_the_original_file_once(tmp_path):
    s = Session.from_demo(48, phy=2, cold_start=True)
    path = _bad_device_csv(tmp_path)
    _o, notes = _messages(s.apply_config_csv, path)
    assert [n.source for n in notes] == ["dev.csv"]
    assert notes[0].message.startswith("DeviceNumber_REG names no device on the bus")
    _o, again = _messages(s._redecode)                 # a re-decode reads nothing new
    assert again == []


def test_a_csv_given_at_construction_is_reported(tmp_path):
    path = _bad_device_csv(tmp_path)
    _s, notes = _messages(Session.from_demo, 48, phy=2, cold_start=True, config_csv=path)
    assert [n.source for n in notes] == ["dev.csv"]


def test_a_legacy_csv_is_named_by_its_own_file_not_the_upgrade_temp(tmp_path):
    p = tmp_path / "old.csv"
    p.write_text("Columns per Row (2-32),8\nFrobnicate,1\n" +
                 "".join(f"{k},{','.join(['0'] * 12)}\n" for k in (
                     "Data Port Enabled", "Data Port Channels", "Data Port Device Number")),
                 encoding="utf-8")
    s = Session.from_demo(48, phy=2, cold_start=True)
    _o, notes = _messages(s.apply_config_csv, str(p))
    assert {n.source for n in notes} == {"old.csv"}
    assert any("Frobnicate" in n.message for n in notes)


def test_a_region_csv_reports_only_the_region_just_imported(tmp_path):
    s = Session.from_demo(6000, phy=2, cold_start=True)
    assert len(s.segments) == 3, "test premise: three config regions"

    def mid(i):
        lo = int(s.segments[i]["start_sample"])
        hi = (int(s.segments[i + 1]["start_sample"]) if i + 1 < len(s.segments)
              else int(s.capture.clock_edges[-1]))
        return (lo + hi) // 2

    first = _bad_device_csv(tmp_path, "r1.csv")
    _o, notes = _messages(s.apply_config_csv_at, mid(1), first)
    assert [n.source for n in notes] == ["r1.csv"]
    clean = demo_config().to_csv_file(str(tmp_path / "r2.csv"))
    _o, notes = _messages(s.apply_config_csv_at, mid(2), clean)
    assert notes == []                                 # r1.csv is not reported again


def test_an_unreadable_region_csv_is_reported_as_skipped(tmp_path):
    _s, notes = _messages(Session.from_demo, 48, phy=2, cold_start=True,
                          csv_sections={0: str(tmp_path / "nothere.csv")})
    assert [n.source for n in notes] == ["nothere.csv"]
    assert notes[0].message.startswith("not read (")
    assert notes[0].message.endswith("region 1 decodes as the wire says.")


# ---------------------------------------------------------------- the window

def _window():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    return MainWindow()


def test_imposing_a_csv_with_a_port_on_no_device_opens_the_import_warnings_window(
        tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    win = _window()
    win.load_demo()
    pump_loads(win)
    monkeypatch.setattr(QMessageBox, "warning",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    win._apply_config_csv_path(_bad_device_csv(tmp_path))
    pump_loads(win)
    dlgs = win.import_warning_dialogs()
    assert len(dlgs) == 1 and not dlgs[0].isModal()
    assert dlgs[0].windowTitle() == "Import warnings — dev.csv"
    assert len(dlgs[0].lines()) == 1
    assert dlgs[0].lines()[0].startswith("dev.csv: DeviceNumber_REG names no device on the bus")
    assert "1 import warning in dev.csv" in win._status.text()


def test_a_visualizer_mode_load_shows_its_warnings_and_a_clean_one_none(tmp_path):
    win = _window()
    win._load_authoring_csv(demo_config().to_csv_file(str(tmp_path / "clean.csv")))
    assert win.import_warning_dialogs() == []
    rows = _rows()
    _set(rows, "Interval_REG", 1, "seven")
    win._load_authoring_csv(_write(tmp_path / "bad.csv", rows))
    dlgs = win.import_warning_dialogs()
    assert [d.lines() for d in dlgs] == [
        ["bad.csv: Interval_REG: 1 cell is not a valid value: DP0 'seven' read as 0."]]


def test_a_legacy_file_cut_off_in_its_per_port_rows_names_the_rows_it_lacks(tmp_path):
    p = tmp_path / "cut.csv"
    full = 12
    p.write_text("Columns per Row (2-32),8\nRow Rate [kHz],3072\n"
                 "Data Port Name" + ",x" * full + "\n"
                 "Data Port Device Number" + ",1" * full + "\n"
                 "Data Port Channels" + ",1" * full + "\n", encoding="utf-8")
    out, notes = _messages(visualizer_csv.normalized_path, str(p))
    try:
        assert [n.message for n in notes] == [
            "legacy rows missing, each taking its default for every data port or the "
            "interface: Data Port Sample Width, Data Port Interval Integer, Data Port Offset, "
            "Data Port Horizontal Start, Data Port Horizontal Count, Source, Data Port "
            "Enabled."]
    finally:
        os.remove(out)


def test_a_legacy_file_without_its_interface_rows_names_them(tmp_path):
    p = tmp_path / "noif.csv"
    p.write_text("Save file using excess one,True\nRows to Draw (1-10240),32\n",
                 encoding="utf-8")
    out, notes = _messages(visualizer_csv.normalized_path, str(p))
    try:
        assert [n.message for n in notes] == [
            "legacy rows missing, each taking its default for every data port or the "
            "interface: Columns per Row, Row Rate [kHz]."]
    finally:
        os.remove(out)


def test_a_complete_legacy_v1_73_file_names_no_missing_row(tmp_path):
    p = tmp_path / "v173.csv"
    rows = ["Columns per Row (2-32),8", "Row Rate [kHz] (1-61440),768"] + [
        name + ",0" * 12 for name in (
            "Data Port Device Number", "Data Port Channels", "Data Port Sample Width",
            "Data Port Interval", "Data Port Offset", "Data Port Horizontal Start",
            "Data Port Horizontal Count")] + [
        name + ",False" * 12 for name in ("Source", "Data Port Enabled")]
    p.write_text("\n".join(rows) + "\n", encoding="utf-8")
    out, notes = _messages(visualizer_csv.normalized_path, str(p))
    try:
        assert not any(n.message.startswith("legacy rows missing") for n in notes), notes
    finally:
        os.remove(out)
