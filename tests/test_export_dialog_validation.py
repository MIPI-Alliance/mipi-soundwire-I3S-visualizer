"""Export dialogs: ranges and the exports refused (an end before the start, no output file,
no signal), signal names, typed resample rates (their unit rule, floor and ceiling), and the
grid image write.

Run: QT_QPA_PLATFORM=offscreen python3 -m pytest tests/test_export_dialog_validation.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from swi3s_studio.session import Session

pytestmark = pytest.mark.gui


def _app():
    return QApplication.instance() or QApplication([])


def _capture_dialog(**kw):
    _app()
    from swi3s_studio.ui.capture_export_dialog import CaptureExportDialog
    return CaptureExportDialog(Session.from_demo(200, cold_start=True, phy=2),
                               default_dir="/tmp", **kw)


def _choose(dlg, key):
    keys = [dlg._range.itemData(i) for i in range(dlg._range.count())]
    dlg._range.setCurrentIndex(keys.index(key))


def test_a_blanked_signal_name_falls_back_to_the_names_the_dialog_was_given():
    dlg = _capture_dialog(clock_name="DP", data_name="DN")
    dlg._clk_name.setText("")
    dlg._dat_name.setText("  ")
    assert dlg.signal_names() == ("DP", "DN")


def test_a_bus_row_range_includes_its_last_row_and_may_be_one_row():
    dlg = _capture_dialog()
    s = dlg._session
    _choose(dlg, "rows")
    dlg._start.setValue(2)
    dlg._end.setValue(10)
    assert dlg.sample_range() == (s.sample_at_bus_row(2), s.sample_at_bus_row(11))
    dlg._start.setValue(5)
    dlg._end.setValue(5)
    s0, s1 = dlg.sample_range()
    assert s0 == s.sample_at_bus_row(5) and s1 > s0
    dlg._start.setValue(0)                                # the defaults: the whole capture
    dlg._end.setValue(dlg._end.maximum())
    assert dlg.sample_range() == (s.sample_at_bus_row(0), s.last_sample() + 1)


def test_a_ui_range_and_a_time_range_keep_the_last_edge():
    dlg = _capture_dialog()
    s = dlg._session
    for key in ("ui", "time"):
        _choose(dlg, key)
        assert dlg._end.value() == dlg._end.maximum()
        assert dlg.sample_range()[1] == s.last_sample() + 1, key
    _choose(dlg, "ui")
    dlg._start.setValue(3)
    dlg._end.setValue(3)
    s0, s1 = dlg.sample_range()
    assert s1 > s0


@pytest.mark.parametrize("key", ["rows", "ui"])
def test_an_end_before_the_start_is_refused(monkeypatch, key):
    told = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    dlg = _capture_dialog()
    dlg._path.setText("/tmp/x.sal")
    _choose(dlg, key)
    dlg._start.setValue(8)
    dlg._end.setValue(4)
    dlg.accept()
    assert told and "before its start" in told[0]
    assert dlg.result() == 0


def test_a_time_range_ending_before_its_start_is_refused(monkeypatch):
    told = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    dlg = _capture_dialog()
    dlg._path.setText("/tmp/x.sal")
    _choose(dlg, "time")
    dlg._start.setValue(0.00002)
    dlg._end.setValue(0.00001)
    dlg.accept()
    assert told == ["The range's end must be after its start."]
    assert dlg.result() == 0


@pytest.mark.parametrize("blank, reason", [("path", "Choose an output file."),
                                           ("signals", "Select at least one signal.")])
def test_an_export_with_no_file_or_no_signal_is_refused(monkeypatch, blank, reason):
    told = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    dlg = _capture_dialog()
    keys = [dlg._format.itemData(i)[0] for i in range(dlg._format.count())]
    dlg._format.setCurrentIndex(next(i for i, k in enumerate(keys) if k != "sal"))
    if blank == "path":
        dlg._path.setText("  ")
    else:
        dlg._clk_on.setChecked(False)
        dlg._dat_on.setChecked(False)
    dlg.accept()
    assert told == [reason]
    assert dlg.result() == 0


def _audio_dialog(rate_hz: float = 48_000.0):
    from swi3s_studio.store.audio_store import AudioStore
    _app()
    from swi3s_studio.ui.audio_export_dialog import AudioExportDialog
    audio = [{"device": 0, "dp": 0, "channel": 0, "sample_size": 16, "value": i,
              "index": i, "start_sample": 100 + i * 50} for i in range(40)]
    return AudioExportDialog(AudioStore.from_audio(audio, {(0, 0): rate_hz}))


@pytest.mark.parametrize("text, why", [
    ("4800O", "is not a sample rate"), ("-8k", "is not a sample rate"),
    ("inf", "is not a sample rate"), ("nan", "is not a sample rate"),
    ("0", "is not a sample rate"), ("fast", "is not a sample rate"),
    ("1e9", "1000000 kHz is above the 768 kHz limit"),
    ("768001", "768.001 kHz is above the 768 kHz limit")])
def test_a_typed_resample_rate_that_is_not_a_usable_rate_is_refused(monkeypatch, text, why):
    told = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    dlg = _audio_dialog()
    dlg._rates[(0, 0)].setEditText(text)
    dlg.accept()
    assert len(told) == 1 and told[0].startswith("Device 0 · DP0: ") and why in told[0], told
    assert dlg.result() == 0


@pytest.mark.parametrize("text, hz", [
    ("48", 48_000), ("44.1", 44_100), ("999", 999_000), ("1000", 1_000),
    ("32000", 32_000), ("48 kHz", 48_000), ("22.05k", 22_050), ("32000 Hz", 32_000),
    ("48 Hz", 48)])
def test_a_typed_rate_with_no_unit_is_khz_below_1000_and_a_unit_always_wins(text, hz):
    from swi3s_studio.ui.audio_export_dialog import _parse_rate
    assert _parse_rate(text) == hz


@pytest.mark.parametrize("text, why", [
    ("48 Hz", "48 Hz is below the 1 kHz minimum"),
    ("999 Hz", "999 Hz is below the 1 kHz minimum"),
    ("0.5", "500 Hz is below the 1 kHz minimum")])
def test_a_typed_rate_below_the_minimum_is_refused_and_not_exported(monkeypatch, text, why):
    told = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    dlg = _audio_dialog()
    dlg._rates[(0, 0)].setEditText(text)
    dlg.accept()
    assert len(told) == 1 and why in told[0], told
    assert "no unit is in kHz below 1000" in told[0]
    assert dlg.result() == 0


def test_the_rate_ceiling_is_the_streams_own_rate_when_that_is_above_768_khz(monkeypatch):
    """A stream faster than 768 kHz may be exported at its own rate, and no faster."""
    told = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    dlg = _audio_dialog(rate_hz=1_536_000.0)
    dlg._rates[(0, 0)].setEditText("1536 kHz")
    dlg.accept()
    assert told == [] and dlg.result() == 1
    dlg = _audio_dialog(rate_hz=1_536_000.0)
    dlg._rates[(0, 0)].setEditText("1537 kHz")
    dlg.accept()
    assert len(told) == 1 and "above the 1536 kHz limit" in told[0]
    assert dlg.result() == 0


@pytest.mark.parametrize("text", ["Native", "", "16 kHz", "22.05 kHz", "96000", "48",
                                  "44.1", "1 kHz"])
def test_a_usable_resample_rate_is_accepted(monkeypatch, text):
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: pytest.fail("refused"))
    dlg = _audio_dialog()
    dlg._rates[(0, 0)].setEditText(text)
    dlg.accept()
    assert dlg.result() == 1


def test_an_unticked_stream_is_not_checked_for_its_rate(monkeypatch):
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: pytest.fail("refused"))
    dlg = _audio_dialog()
    dlg._checks[(0, 0)].setChecked(False)
    dlg._rates[(0, 0)].setEditText("junk")
    dlg.accept()
    assert dlg.result() == 1


def test_grid_image_export_raises_when_nothing_could_be_written(tmp_path):
    _app()
    from swi3s_studio.ui.grid_view import GridView
    s = Session.from_demo(64)
    grid = GridView()
    grid.set_cells(s.grid_cells(), s.column_count)
    for name in ("g.png", "g.svg"):
        with pytest.raises(OSError):
            grid.export_image(str(tmp_path / "missing" / name))
    assert grid.export_image(str(tmp_path / "bus_grid")) == str(tmp_path / "bus_grid")
    assert (tmp_path / "bus_grid").read_bytes().startswith(b"\x89PNG\r\n\x1a\n")   # no suffix: PNG
