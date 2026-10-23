"""Import warnings from a Visualizer config CSV: the native loader (swi3score.csv_warnings),
the format upgrade (ingest.visualizer_csv), the Python parsers (BusConfig.from_csv,
CSVHandler via viz_engine and the CLI), Session's config imports, and the window.

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

pytestmark = pytest.mark.skipif(not hasattr(swi3score, "csv_warnings"),
                                reason="the built core predates csv_warnings; rebuild it")

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
    assert swi3score.csv_warnings(os.path.join(HERE, "fixtures", "flow_control_demo.csv")) == []


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


def test_an_unopenable_path_has_no_warnings_and_the_core_is_unchanged(tmp_path):
    assert swi3score.csv_warnings(str(tmp_path / "missing.csv")) == []
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
        "GuardEnable_REG: 1 cell is not a valid value: DP0 'Ture' read as False.",
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
