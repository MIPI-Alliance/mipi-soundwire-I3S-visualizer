"""Import warnings from a Visualizer config CSV: the native loader (swi3score.csv_warnings),
the format upgrade (ingest.visualizer_csv), the Python parsers (BusConfig.from_csv,
CSVHandler via viz_engine and the CLI), a Reserved CDS_TailWidth 3 on each of those paths,
the legacy CDS guard, polarity and tail width carried to every source, Session's config
imports, and the window.

Every input is synthetic: a config the app writes (demo_config) with cells edited."""
import csv
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
            if r[0] not in ("CDS_DriveTypePerSource", "CDS_EndDriveEarlyPerSource")]
    assert swi3score.csv_warnings(_write(tmp_path / "lacking.csv", rows)) == [
        "2 rows are missing and take their defaults: CDS_DriveTypePerSource, "
        "CDS_EndDriveEarlyPerSource."]


def test_a_port_with_no_device_on_the_bus_is_reported_disabled(tmp_path):
    rows = _rows()
    _set(rows, "DeviceNumber_REG", 1, "12")              # DP0 is enabled with channels
    w = swi3score.csv_warnings(_write(tmp_path / "dev.csv", rows))
    assert w == ["DeviceNumber_REG names no device on the bus (0..11) for DP0 (12); that port "
                 "is disabled and left out of the audio, the grid and the registers."]
    assert swi3score.registers_from_csv(str(tmp_path / "dev.csv")) == []


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
    _set(rows, "CDS_TailWidthPerSource", 1, "5")
    _set(rows, "CDS_TailWidthPerSource", 3, "-1")
    _set(rows, "CDS_DriveTypePerSource", 2, "2")
    assert swi3score.csv_warnings(_write(tmp_path / "fit.csv", rows)) == [
        "SkippingDenominator_REG 5000 does not fit its 12-bit register field (0..4095); the "
        "expected registers carry its low 12 bits (904) and the decode uses the value as "
        "written.",
        "CDS_BitWidth_REG 9 does not fit its 3-bit register field (0..7); the expected "
        "registers carry its low 3 bits (1) and the decode uses the value as written.",
        "CDS_TailWidthPerSource does not fit its 2-bit register field (0..3) for Manager = 5, "
        "Device 1 = -1; the expected registers carry its low 2 bits.",
        "CDS_DriveTypePerSource does not fit its 1-bit register field (0..1) for Device 0 = 2; "
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
        assert [n.source for n in notes] == ["old.csv", "old.csv"]
        assert notes[0].message == "1 legacy field is not recognised and was skipped: Frobnicate."
        assert notes[1].message == (
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


def test_the_cli_prints_the_warnings(tmp_path, capsys):
    from swi3s_studio import cli
    rows = _rows() + [["Frobnicate", "1"]]
    code = cli.run_headless(_write(tmp_path / "odd.csv", rows), str(tmp_path / "m.json"))
    assert code == 0
    err = capsys.readouterr().err
    assert ("warning: odd.csv: 1 field is not recognised and was ignored: Frobnicate "
            f"(line {len(rows)}).") in err


# ---------------------------------------------------------------- Reserved CDS_TailWidth 3

RESERVED = "CDS_TailWidth 3 is Reserved (0..2 UI are defined) for "


def _tail_rows(cells):
    """The demo rows with the per-source tail row set to `cells` (source index -> text)."""
    rows = _rows()
    for index, text in cells.items():
        _set(rows, "CDS_TailWidthPerSource", index + 1, text)
    return rows


def _scalar_tail_rows(width):
    """The demo rows with the legacy scalar tail row in place of the per-source one."""
    return [r for r in _rows() if r[0] != "CDS_TailWidthPerSource"] + [
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
        "CDS_TailWidthPerSource: " + RESERVED + "Device 1 = 3; the value is kept as written, "
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
        "CDS_TailWidthPerSource does not fit its 2-bit register field (0..3) for "
        f"Device 0 = {width}; the expected registers carry its low 2 bits."]


def test_bus_config_names_every_source_of_a_scalar_reserved_tail_width(tmp_path):
    cfg, notes = _messages(BusConfig.from_csv,
                           _write(tmp_path / "t.csv", _scalar_tail_rows(3)))
    assert cfg.cds_tail == [3] * 13
    reserved = [n.message for n in notes if RESERVED in n.message]
    assert len(reserved) == 1
    assert reserved[0].startswith("CDS_TailWidthPerSource: " + RESERVED + "Manager = 3, "
                                  "Device 0 = 3, Device 1 = 3 and 10 more;")
    _cfg, notes = _messages(BusConfig.from_csv, _write(tmp_path / "u.csv", _scalar_tail_rows(2)))
    assert not [n for n in notes if "Reserved" in n.message]


def test_the_csv_handler_keeps_a_reserved_tail_width_and_reports_it_once(tmp_path):
    from swi3s_studio.model.viz_engine import build_bus_model
    rows = _tail_rows({0: "3", 4: "3", 2: "1"})
    line = 1 + next(i for i, r in enumerate(rows) if r[0] == "CDS_TailWidthPerSource")
    _m, notes = _messages(build_bus_model, _write(tmp_path / "t.csv", rows), 4)
    assert [(n.source, n.message) for n in notes] == [(
        "t.csv", f"Line {line} (CDS_TailWidthPerSource): CDS_TailWidth 3 is Reserved (0..2 UI "
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
    want = ("CDS_TailWidthPerSource: " + RESERVED + "Device 1 = 3; the value is kept as "
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
    want = ("CDS_TailWidthPerSource: " + RESERVED + listed + "; the value is kept as written, "
            "the grid draws a 3-UI tail and the expected registers carry 3.")
    assert swi3score.csv_warnings(path) == [want]
    with import_notes.collect():
        cfg = BusConfig.from_csv(path)
    assert reserved_cds_tail_note(cfg.reserved_cds_tail_sources()) == want


def test_the_native_loader_names_every_source_of_a_scalar_reserved_tail_width(tmp_path):
    warnings = swi3score.csv_warnings(_write(tmp_path / "t.csv", _scalar_tail_rows(3)))
    assert [w for w in warnings if "Reserved" in w] == [
        "CDS_TailWidthPerSource: " + RESERVED + "Manager = 3, Device 0 = 3, Device 1 = 3 and 10 "
        "more; the value is kept as written, the grid draws a 3-UI tail and the expected "
        "registers carry 3."]
    assert not [w for w in swi3score.csv_warnings(_write(tmp_path / "u.csv", _scalar_tail_rows(2)))
                if "Reserved" in w]


@pytest.mark.parametrize("width", ["4", "7", "-1"])
def test_the_native_loader_reports_a_wide_tail_width_only_as_wide(tmp_path, width):
    assert swi3score.csv_warnings(_write(tmp_path / "t.csv", _tail_rows({1: width}))) == [
        "CDS_TailWidthPerSource does not fit its 2-bit register field (0..3) for "
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
        "old.csv", "CDS_TailWidthPerSource: " + RESERVED + "Manager = 3, Device 0 = 3, "
        "Device 1 = 3 and 10 more; the value is kept as written, the grid draws a 3-UI tail "
        "and the expected registers carry 3.")]


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


@pytest.mark.parametrize("enabled,polarity,guard", [
    ("True", "False", 1), ("True", "True", 2), ("False", "False", 0), ("yes", "1", 2)])
def test_a_legacy_guard_and_polarity_are_carried_to_every_source(tmp_path, enabled, polarity,
                                                                 guard):
    rows, cfg, _native, notes = _legacy_rows(
        tmp_path / "old.csv", [f"CDS Guard Enabled,{enabled}", f"CDS Guard Polarity,{polarity}"])
    assert notes == []
    assert rows["CDS_GuardEnabledPerSource"] == ["1" if guard else "0"] * 13
    assert rows["CDS_GuardPolarityPerSource"] == ["1" if guard == 2 else "0"] * 13
    assert cfg.cds_guard == [guard] * 13
    from swi3s_studio.model.viz_engine import build_bus_model
    p = tmp_path / "old.csv"
    out = visualizer_csv.normalized_path(str(p))
    try:
        (_m, iface, _viz), _n = _messages(build_bus_model, out, 4)
    finally:
        os.remove(out)
    assert iface.CDS_Guard_PerSource == [guard] * 13


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


def test_a_legacy_polarity_on_a_disabled_guard_is_reported_and_written_as_guard_0(tmp_path):
    rows, cfg, _native, notes = _legacy_rows(
        tmp_path / "old.csv", ["CDS Guard Enabled,False", "CDS Guard Polarity,True"])
    assert notes == ["1 legacy value did not fit and was changed: CDS Guard Polarity is true "
                     "while CDS Guard Enabled is false; a polarity is kept only with its guard, "
                     "so false is used."]
    assert rows["CDS_GuardPolarityPerSource"] == ["0"] * 13
    assert cfg.cds_guard == [0] * 13


def test_a_legacy_file_without_guard_rows_upgrades_as_before(tmp_path):
    """No guard row: every source is off, and adding the guard rows changes only the two
    per-source guard rows of the upgraded file."""
    plain, cfg, _native, notes = _legacy_rows(tmp_path / "plain.csv", ["CDS Tail Width,2"])
    assert notes == [] and cfg.cds_guard == [0] * 13
    assert plain["CDS_GuardEnabledPerSource"] == plain["CDS_GuardPolarityPerSource"] == ["0"] * 13
    guarded, _cfg, _native, _notes = _legacy_rows(
        tmp_path / "guarded.csv",
        ["CDS Tail Width,2", "CDS Guard Enabled,True", "CDS Guard Polarity,True"])
    changed = {k for k in plain.keys() | guarded.keys() if plain.get(k) != guarded.get(k)}
    assert changed == {"CDS_GuardEnabledPerSource", "CDS_GuardPolarityPerSource"}


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
