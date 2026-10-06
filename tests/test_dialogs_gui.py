"""The dialogs, driven as a user would: each is opened by its real menu handler (or built
as its handler builds it), filled in through its own widgets, and accepted or cancelled.

Before this, four dialogs had 0-9% coverage and every handler that opens one was
unexercised past the `exec()`: a test that reached a modal simply hung offscreen. The
conftest guard now fails such a test instead, and `_drive` below is the sanctioned way
through: it replaces `exec` on one dialog class with a function that plays the user.
"""
import os
import wave

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QMessageBox,
)


def _app():
    return QApplication.instance() or QApplication([])


def _drive(monkeypatch, cls, user):
    """Make `cls.exec()` run `user(dialog)` instead of a modal loop and return what it
    returns (Accepted / Rejected). The dialog is fully built and live when `user` runs."""
    seen = []

    def exec_(self):
        seen.append(self)
        return user(self)
    monkeypatch.setattr(cls, "exec", exec_)
    return seen


def _win():
    from swi3s_studio.ui.main_window import MainWindow
    _app()
    w = MainWindow()
    w.load_demo()
    return w


# ---- Export Audio ----

def test_audio_export_rate_parsing():
    from swi3s_studio.ui.audio_export_dialog import _parse_rate
    assert _parse_rate("Native") is None and _parse_rate("") is None
    assert _parse_rate("48 kHz") == 48_000
    assert _parse_rate("22.05 kHz") == 22_050
    assert _parse_rate("32000") == 32_000
    assert _parse_rate("fast") is None                  # typed nonsense: native, not a crash


def test_the_audio_export_dialog_lists_every_stream_and_seeds_the_playback_rate():
    from swi3s_studio.ui.audio_export_dialog import AudioExportDialog
    win = _win()
    store = win._audio_store
    streams = list(store.streams())
    assert len(streams) >= 2
    first = streams[0]
    dlg = AudioExportDialog(store, visible_index_range=(10, 20),
                            default_rates={first: 16_000}, parent=win)
    assert dlg.selected_streams() == streams             # all ticked by default
    assert dlg.target_rate(*first) == 16_000             # export matches playback
    assert dlg.target_rate(*streams[1]) is None          # native
    assert dlg.index_range() is None                      # whole capture first
    dlg._range.setCurrentIndex(1)
    assert dlg.index_range() == (10, 20)
    dlg._rates[first].setEditText("22.05 kHz")           # a rate typed, not picked
    assert dlg.target_rate(*first) == 22_050


def test_export_audio_writes_the_chosen_stream_at_the_chosen_rate(tmp_path, monkeypatch):
    from swi3s_studio.ui.audio_export_dialog import AudioExportDialog
    win = _win()
    keep = list(win._audio_store.streams())[0]

    def user(dlg):
        for key, cb in dlg._checks.items():
            cb.setChecked(key == keep)
        combo = dlg._rates[keep]
        combo.setCurrentIndex(combo.findText("16 kHz"))
        return QDialog.Accepted
    _drive(monkeypatch, AudioExportDialog, user)
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: str(tmp_path))
    told = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[1:3]))
    win.export_audio()
    written = sorted(os.listdir(tmp_path))
    assert written == [f"swi3s_dev{keep[0]}_dp{keep[1]}.wav"]
    with wave.open(str(tmp_path / written[0])) as w:
        assert w.getframerate() == 16_000
    assert told and told[0][0] == "Audio exported" and "16 kHz" in told[0][1]


def test_export_audio_with_nothing_ticked_says_so_and_writes_nothing(tmp_path, monkeypatch):
    from swi3s_studio.ui.audio_export_dialog import AudioExportDialog
    win = _win()

    def user(dlg):
        for cb in dlg._checks.values():
            cb.setChecked(False)
        return QDialog.Accepted
    _drive(monkeypatch, AudioExportDialog, user)
    told = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    win.export_audio()
    assert told == ["No streams selected."] and os.listdir(tmp_path) == []


def test_cancelling_export_audio_asks_for_no_directory(monkeypatch):
    from swi3s_studio.ui.audio_export_dialog import AudioExportDialog
    win = _win()
    _drive(monkeypatch, AudioExportDialog, lambda dlg: QDialog.Rejected)
    win.export_audio()                  # the guard fails the test if a picker opens


# ---- Open Analog CSV ----

# The analog CSV dialog's job (channels, Schmitt thresholds) is the Open Capture dialog's
# now: tests/test_open_capture_dialog.py.


# ---- Import Visualizer CSV ----

def test_the_config_dialog_needs_a_source_and_authoring_can_only_compare():
    from swi3s_studio.ui.open_config_dialog import OpenVisualizerConfigDialog
    _app()
    dlg = OpenVisualizerConfigDialog(None, has_authoring=True)
    dlg._accept()
    assert dlg.result() != QDialog.Accepted              # no file and not authoring
    dlg._from_auth.setChecked(True)
    assert not dlg._act_update.isEnabled() and dlg._act_compare.isChecked()
    dlg._accept()
    assert dlg.result() == QDialog.Accepted
    assert dlg.use_authoring and dlg.do_compare and not dlg.do_update


def test_the_config_dialog_without_authoring_offers_only_a_file(monkeypatch, tmp_path):
    from swi3s_studio.ui.open_config_dialog import OpenVisualizerConfigDialog
    _app()
    dlg = OpenVisualizerConfigDialog(None, has_authoring=False)
    assert not dlg._from_auth.isEnabled()
    target = str(tmp_path / "cfg.csv")
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (target, ""))
    dlg._browse()
    dlg._accept()
    assert dlg.result() == QDialog.Accepted
    assert (dlg.path, dlg.use_authoring, dlg.do_update) == (target, False, True)


def test_importing_a_capture_csv_as_a_config_is_refused(monkeypatch, tmp_path):
    """A digital capture CSV ends in .csv too; imposing it would silently empty the grid."""
    from swi3s_studio.ui.open_config_dialog import OpenVisualizerConfigDialog
    win = _win()
    capture_csv = tmp_path / "capture.csv"
    capture_csv.write_text("Time,D0,D1\n0,0,1\n1e-6,1,0\n")

    def user(dlg):
        dlg._path_edit.setText(str(capture_csv))
        dlg._accept()
        return dlg.result()
    _drive(monkeypatch, OpenVisualizerConfigDialog, user)
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    session, before = win._session, dict(win._session.source)
    win.open_visualizer_config()
    assert warned and "isn't a Visualizer config" in warned[0]
    assert win._session is session and win._session.source == before   # nothing imposed


# ---- Import Peripheral Register Map ----

def _regmap_xml(tmp_path):
    from test_regmap_import import FIXTURE
    path = tmp_path / "chip.xml"
    path.write_text(FIXTURE)
    return str(path)


def test_the_regmap_dialog_previews_an_import_and_binds_the_device(tmp_path, monkeypatch):
    from swi3s_studio.ui.regmap_import_dialog import RegisterMapImportDialog
    _app()
    dlg = RegisterMapImportDialog(None, device=3, devices=[1, 3, 5])
    ok = dlg._buttons.button(dlg._buttons.StandardButton.Ok)
    assert not ok.isEnabled()                            # nothing imported yet
    path = _regmap_xml(tmp_path)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (path, ""))
    dlg._browse()
    assert ok.isEnabled() and dlg.pmap is not None
    assert f"{len(dlg.pmap.registers)} registers" in dlg._summary.text()
    assert dlg._preview.topLevelItemCount() == len(dlg.pmap.registers)
    def amp_fields():
        return [f.name for r in dlg.pmap.registers if r.name == "AMP_LEVEL" for f in r.fields]
    assert amp_fields() == ["amp_lvl"]                   # amp_lvl[5:0], one ranged field
    dlg._coalesce.setChecked(False)                      # re-imports, bit by bit
    assert len(amp_fields()) == 6
    dlg.accept()
    assert dlg.device == 3


def test_the_regmap_dialog_round_trips_a_normalized_json(tmp_path, monkeypatch):
    from test_regmap_import import FIXTURE

    from swi3s_studio.model.regmap_import import import_text
    from swi3s_studio.ui.regmap_import_dialog import RegisterMapImportDialog
    _app()
    pmap, _ = import_text(FIXTURE, "chip", coalesce=True)
    path = tmp_path / "chip.json"
    path.write_text(pmap.to_json())
    dlg = RegisterMapImportDialog(None)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(path), ""))
    dlg._browse()
    assert "normalized json" in dlg._summary.text()
    assert [r.name for r in dlg.pmap.registers] == [r.name for r in pmap.registers]


def test_a_broken_regmap_says_why_and_cannot_be_accepted(tmp_path, monkeypatch):
    from swi3s_studio.ui.regmap_import_dialog import RegisterMapImportDialog
    _app()
    bad = tmp_path / "broken.json"
    bad.write_text("{ not json")
    dlg = RegisterMapImportDialog(None)
    monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: (str(bad), ""))
    dlg._browse()
    assert dlg.pmap is None and "Import failed" in dlg._summary.text()
    assert not dlg._buttons.button(dlg._buttons.StandardButton.Ok).isEnabled()


# ---- Set Offset… (a Link's timeline label) ----

def _two_links():
    win = _win()
    win.load_demo_links()
    return win


def test_set_offset_converts_between_units_and_applies_in_picoseconds(monkeypatch):
    win = _two_links()
    shown = []

    def user(dlg):
        value = dlg.findChild(QDoubleSpinBox)
        unit = dlg.findChild(QComboBox)
        value.setValue(5.0)                              # 5 µs ...
        unit.setCurrentIndex(unit.findText("ns"))        # ... is shown as 5000 ns
        shown.append(value.value())
        value.setValue(value.value() + 0.001)            # 1 ps more, in ns
        return QDialog.Accepted
    _drive(monkeypatch, QDialog, user)
    win.set_link_offset_dialog()
    assert shown == [5000.0]
    assert win.links[win.links.active_index].offset_ps == 5_000_001


def test_cancelling_set_offset_changes_nothing(monkeypatch):
    win = _two_links()
    before = [e.offset_ps for e in win.links]
    _drive(monkeypatch, QDialog, lambda dlg: QDialog.Rejected)
    win.set_link_offset_dialog()
    assert [e.offset_ps for e in win.links] == before


def test_set_offset_with_one_link_opens_nothing():
    win = _win()
    win.set_link_offset_dialog()                          # the guard fails on a dialog
