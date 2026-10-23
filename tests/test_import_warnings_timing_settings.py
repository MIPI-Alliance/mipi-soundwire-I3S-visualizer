"""Import warnings from Timing Calculator settings (File ▸ Open Timing Settings, a
workspace's timing inputs, and the Example side's Load Example): a key this version does not
read, a choice no selector offers, and a number outside its field's range are reported."""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFileDialog

from swi3s_studio import import_notes


def _tv():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.timing_view import TimingView
    return TimingView()


def test_saved_settings_read_back_without_warnings_on_every_bus_choice():
    tv = _tv()
    for i in range(tv._man_spec_combo.count()):
        tv._man_spec_combo.setCurrentIndex(i)
        d = tv.to_dict()
        with import_notes.collect() as notes:
            _tv().set_from_dict(d, source="t.json")
        assert notes == [], tv._man_spec_combo.itemData(i)


def test_unknown_keys_choices_and_out_of_range_numbers_are_reported():
    tv = _tv()
    d = tv.to_dict()
    row_key = next(k for k in d if k.endswith("::max"))
    d.update({"color_scheme": "blue", "man_spec": "NO_SUCH_PHY", "fclk_target": 1e9,
              row_key: 1e12, "phy": 2, "spec::man_shape": d["man_shape"]})
    with import_notes.collect() as notes:
        _tv().set_from_dict(d, source="t.json")
    text = [n.message for n in notes]
    assert {n.source for n in notes} == {"t.json"}
    assert text[0] == "1 setting(s) this version does not read, ignored: color_scheme"
    assert text[1].startswith("'man_spec' 'NO_SUCH_PHY' is not one of the choices offered;")
    assert text[2].startswith("2 value(s) outside their field's range set to the nearest end: "
                              "fclk_target = 1e+09 (read as ")
    assert f"{row_key} = 1e+12" in text[2]


def test_without_a_source_set_from_dict_reports_nothing():
    with import_notes.collect() as notes:
        _tv().set_from_dict({"man_spec": "NO_SUCH_PHY"})
    assert notes == []


def _window():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    return MainWindow()


def test_open_timing_settings_shows_its_warnings(tmp_path, monkeypatch):
    win = _window()
    d = win._timing_page.to_dict()
    d["launch_mode"] = "telepathy"
    path = tmp_path / "timing_settings.json"
    path.write_text(json.dumps(d), encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    win.open_timing_settings()
    dlgs = win.import_warning_dialogs()
    assert len(dlgs) == 1
    assert dlgs[0].windowTitle() == "Import warnings — timing_settings.json"
    assert dlgs[0].lines()[0].startswith(
        "timing_settings.json: 'launch_mode' 'telepathy' is not one of the choices offered")


def test_load_example_reports_rows_it_does_not_have(tmp_path, monkeypatch):
    win = _window()
    tv = win._timing_page
    payload = tv._serialize_side("proposed")
    name = next(iter(payload))
    payload[name]["max"] = 1e12
    payload["NoSuchRow"] = {"min": 0, "max": 1}
    path = tmp_path / "example.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    tv._load_proposed()
    dlgs = win.import_warning_dialogs()
    assert len(dlgs) == 1 and dlgs[0].windowTitle() == "Import warnings — example.json"
    lines = dlgs[0].lines()
    assert ("example.json: 1 row(s) this version does not have or cannot read, ignored: "
            "NoSuchRow") in lines
    assert any(line.startswith(f"example.json: 1 value(s) outside their field's range set to "
                               f"the nearest end: {name} max = 1e+12") for line in lines)


def test_load_example_of_a_saved_example_reports_nothing(tmp_path, monkeypatch):
    win = _window()
    tv = win._timing_page
    path = tmp_path / "example.json"
    path.write_text(json.dumps(tv._serialize_side("proposed")), encoding="utf-8")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    tv._load_proposed()
    assert win.import_warning_dialogs() == []
