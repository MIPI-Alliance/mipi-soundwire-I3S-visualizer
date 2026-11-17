"""Naming a data port: Audio ▸ right-click a channel ▸ Rename…. The name is the Link's
(Session.dp_names, in its source descriptor, so a workspace keeps it) and every pane that
labels the port shows it: the Audio channels and track titles, the Samples Port column and
filter, the Capture pane's port legend and the Bus Grid's colour key."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QInputDialog, QMenu

from swi3s_studio.session import Session
from swi3s_studio.ui.port_names import port_identity, port_label, with_name

pytestmark = pytest.mark.gui


def test_a_port_is_labelled_by_its_name_or_its_numbers():
    names = {(1, 2): "Mic L"}
    assert port_label(names, 1, 2) == "Mic L" and port_label(names, 1, 3) == "Dev1 DP3"
    assert port_identity(names, 1, 2) == "Mic L (Dev1 DP2)"
    assert with_name(names, 1, 2, "Device 1 · DP2") == "Mic L · Device 1 · DP2"
    assert with_name(names, 1, 3, "Device 1 · DP3") == "Device 1 · DP3"


def test_a_name_is_kept_in_the_source_and_survives_a_reopen():
    s = Session.from_demo(300, phy=2)
    s.set_dp_name(0, 1, "  Mic L ")
    assert s.dp_names == {(0, 1): "Mic L"} and s.source["dp_names"] == {"0.1": "Mic L"}
    assert s.dp_display_map()[(0, 1)]["name"] == "Mic L"       # the grid's colour key
    again = Session.from_source(dict(s.source))
    assert again.dp_names == {(0, 1): "Mic L"}
    again.set_dp_name(0, 1, "")
    assert again.dp_names == {} and "dp_names" not in again.source


def test_a_saved_name_that_is_not_text_is_dropped_and_reported():
    from swi3s_studio import import_notes
    source = dict(Session.from_demo(300, phy=2).source)
    source["dp_names"] = {"0.1": None, "0.2": 7, "0.3": ["x"], "0.0": "Mic L"}
    with import_notes.collect() as notes:
        s = Session.from_source(source)
    assert s.dp_names == {(0, 0): "Mic L"}
    assert s.source["dp_names"] == {"0.0": "Mic L"}
    text = [n.message for n in notes if "dp_names" in n.message]
    assert len(text) == 1 and all(k in text[0] for k in ("'0.1'", "'0.2'", "'0.3'"))


@pytest.fixture
def win():
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    w = MainWindow()
    w.resize(1400, 900)
    w.show()
    w.load_demo()
    QApplication.processEvents()
    return w


def _rename(win, monkeypatch, key, answer):
    av = win._audio_view
    monkeypatch.setattr(QMenu, "exec", lambda self, *_a: next(
        a for a in self.actions() if a.text().startswith("Rename")))
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: answer))
    av._stream_menu(av._checks[key], av._checks[key].rect().center(), *key)
    QApplication.processEvents()


def test_rename_relabels_every_pane(win, monkeypatch):
    av = win._audio_view
    key = sorted(av._checks)[1]
    dev, dp, ch = key
    _rename(win, monkeypatch, key, ("Mic L", True))
    assert win._session.dp_names == {(dev, dp): "Mic L"}
    assert av._checks[key].text() == f"Mic L CH{ch}"
    assert f"Dev{dev} DP{dp}" in av._checks[key].toolTip()
    titles = [p.titleLabel.text for p, _c, d, q, _k, _n in av._plots if (d, q) == (dev, dp)]
    assert titles and all(t.startswith(f"Mic L · Dev{dev} · DP{dp}") for t in titles)
    other = sorted(av._checks)[0]
    assert av._checks[other].text().startswith(f"Dev{other[0]} DP{other[1]}")
    sv = win._sample_view
    assert "Mic L" in [a.text() for a in sv._port_cb._menu.actions()]
    win._raw_view._show_ports = True
    win._raw_view._refresh_port_legend()
    legend = [label.text for _s, label in win._raw_view._port_legend.items]
    assert "Mic L" in legend
    assert win._session.dp_display_map()[(dev, dp)]["name"] == "Mic L"


def test_cancel_changes_nothing_and_an_empty_name_clears_it(win, monkeypatch):
    av = win._audio_view
    key = sorted(av._checks)[0]
    _rename(win, monkeypatch, key, ("Mic L", False))           # cancelled
    assert win._session.dp_names == {}
    _rename(win, monkeypatch, key, ("Mic L", True))
    _rename(win, monkeypatch, key, ("", True))
    assert win._session.dp_names == {}
    assert av._checks[key].text().startswith(f"Dev{key[0]} DP{key[1]}")


def test_a_re_decode_keeps_the_names(win, monkeypatch):
    av = win._audio_view
    key = sorted(av._checks)[2]
    _rename(win, monkeypatch, key, ("Probe", True))
    win.load_session(win._session)                             # an SSP step, an override
    QApplication.processEvents()
    assert win._audio_view._checks[key].text().startswith("Probe CH")


def test_the_audio_channel_list_is_as_wide_as_its_longest_entry(win, monkeypatch):
    """The channel list starts at its longest entry's width (checkbox and text, plus the
    list's margins and scroll bar), follows a rename while it is at that width, and keeps a
    width dragged on the splitter or restored from a workspace."""
    from swi3s_studio.ui import view_state
    from swi3s_studio.ui.audio_view import _CHANNEL_LIST_MIN
    av = win._audio_view

    def list_width():
        return av._splitter.sizes()[0]
    widest = max(cb.sizeHint().width() for cb in av._checks.values())
    assert list_width() == av.channel_list_fit_width() >= max(widest, _CHANNEL_LIST_MIN)
    assert list_width() < widest + 60                       # no wasted space before the tracks
    short = list_width()
    key = sorted(av._checks)[0]
    _rename(win, monkeypatch, key, ("A Much Longer Data Port Name", True))
    assert list_width() == av.channel_list_fit_width() > short
    assert list_width() >= av._checks[key].sizeHint().width()
    total = sum(av._splitter.sizes())
    av._splitter.setSizes([333, total - 333])               # a drag on the splitter
    _rename(win, monkeypatch, key, ("AMP1", True))
    assert list_width() == 333
    view_state.restore_audio(av, {"channel_list_width": 222})
    assert list_width() == 222
    win.load_session(win._session)                          # a re-decode keeps it
    QApplication.processEvents()
    assert list_width() == 222


def test_the_grid_key_widens_to_fit_a_name(win):
    """A swatch is as wide as its label, and no narrower than 46 px, and the swatches do
    not overlap."""
    from PySide6.QtGui import QFontMetricsF
    s = win._session
    win.cursor.set_sample(int(s.segments[-1]["start_sample"]) + 200_000)
    s.set_dp_name(0, 1, "Left Microphone Array")
    win._update_grid_for_cursor(win.cursor.sample)
    QApplication.processEvents()
    gv = win._grid_view
    hits = dict((port, rect) for rect, port in gv._key_hits)
    need = QFontMetricsF(gv._f_key).horizontalAdvance("Left Microphone Array")
    assert hits[(0, 1)].width() >= need
    assert all(r.width() >= 46 for r in hits.values())
    rects = sorted(hits.values(), key=lambda r: r.left())
    assert all(a.right() < b.left() for a, b in zip(rects, rects[1:]))


def test_the_menu_item_reads_rename(win, monkeypatch):
    av = win._audio_view
    key = sorted(av._checks)[0]
    seen = []

    def look(self, *_a):
        seen.extend(a.text() for a in self.actions())
        return None

    monkeypatch.setattr(QMenu, "exec", look)
    av._stream_menu(av._checks[key], av._checks[key].rect().center(), *key)
    assert "Rename…" in seen


def _named(win, monkeypatch, dev=0, dp=1, name="Mic L"):
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: (name, True)))
    win.rename_data_port(dev, dp)
    QApplication.processEvents()


def test_the_audio_menus_name_the_port(win, monkeypatch):
    _named(win, monkeypatch)
    win._build_decimation_menu()
    subs = [a.text() for a in win._play_rate_menu.actions()]
    assert "Mic L · Device 0 · DP1" in subs and "Device 0 · DP0" in subs
    win._build_processing_menu()
    items = [a.text() for a in win._process_menu.actions()]
    assert "Mic L · Device 0 · DP1…" in items and "Device 0 · DP0…" in items


def test_a_name_with_an_ampersand_is_shown_literally_in_menus_and_checkboxes(win, monkeypatch):
    """'&' in a port name is doubled where Qt reads one as a mnemonic marker: the audio
    menus, the Timing filter's port item and the Audio channel checkbox."""
    _named(win, monkeypatch, dp=2, name="A&B")
    win._build_decimation_menu()
    assert "A&&B · Device 0 · DP2" in [a.text() for a in win._play_rate_menu.actions()]
    win._build_processing_menu()
    assert "A&&B · Device 0 · DP2…" in [a.text() for a in win._process_menu.actions()]
    labels = [i["action"].text() for i in win._eye_view._filter_items if i.get("port")]
    assert "A&&B · Device 0 DP2" in labels
    win.load_session(win._session)                      # the filter built with the name
    QApplication.processEvents()
    labels = [i["action"].text() for i in win._eye_view._filter_items if i.get("port")]
    assert "A&&B · Device 0 DP2" in labels
    av = win._audio_view
    checks = [cb.text() for (d, p, _c), cb in av._checks.items() if (d, p) == (0, 2)]
    assert checks and all(t.startswith("A&&B CH") for t in checks)


def test_the_dialogs_name_the_port(win, monkeypatch):
    from swi3s_studio.ui.audio_export_dialog import AudioExportDialog
    from swi3s_studio.ui.audio_process_dialog import StreamProcessingDialog
    _named(win, monkeypatch)
    names = win._session.dp_names
    dlg = StreamProcessingDialog(win._audio_store, 0, 1, preview=lambda _p: None,
                                 parent=win, names=names)
    assert dlg.windowTitle() == "Filter & Gain: Mic L · Dev0 DP1"
    exp = AudioExportDialog(win._audio_store, parent=win, names=names)
    assert exp._checks[(0, 1)].text().startswith("Mic L · Device 0 · DP1  (")
    assert exp._checks[(0, 0)].text().startswith("Device 0 · DP0  (")
    seen = []
    from PySide6.QtWidgets import QDialog
    monkeypatch.setattr(QDialog, "exec", lambda self: seen.append(self.windowTitle()) or 0)
    win.edit_stream_processing(0, 1)
    assert seen == ["Filter & Gain: Mic L · Dev0 DP1"]          # the window passes them


def test_the_timing_filter_names_the_port_and_keeps_what_was_checked(win, monkeypatch):
    ev = win._eye_view
    item = next(i for i in ev._filter_items if i.get("port") == (0, 2))
    item["action"].setChecked(True)
    _named(win, monkeypatch, dp=2)
    assert item["action"].text() == "Mic L · Device 0 DP2" and item["action"].isChecked()
    # A rebuild (a re-decode, a reopened workspace) labels it the same way, so a filter
    # saved by its labels is found again.
    win.load_session(win._session)
    QApplication.processEvents()
    labels = [i["action"].text() for i in win._eye_view._filter_items if i.get("port")]
    assert "Mic L · Device 0 DP2" in labels


# ---- View Bus Grid in Visualizer carries the names over ----

def _name_ports(win, names):
    for (dev, dp), name in names.items():
        win._session.set_dp_name(dev, dp, name)


def _csv_rows(path):
    """[(field, values)] of a saved settings CSV, in file order."""
    import csv
    with open(path, newline="", encoding="utf-8") as f:
        return [(r[0], r[1:]) for r in csv.reader(f)]


def _chips(win):
    return [c.text() for c in win._authoring._dp_name]


def test_the_decoder_config_takes_the_names_by_device_and_port_number():
    from swi3s_studio.model.bus_config import BusConfig

    def port(dev, number, enabled=True):
        return {"enabled": enabled, "device_number": dev, "dp_number": number, "enable_ch": 3}
    d = {"dataports": [port(0, 0, enabled=False), port(1, 2), port(2, 2), port(-1, 5)]}
    names = {(1, 2): "Mic L", (-1, 5): " Manager Out ", (0, 0): "Never Packed"}
    cfg, n = BusConfig.from_decoder_config(d, names=names)
    assert n == 3
    assert [dp.name for dp in cfg.dataports[:4]] == ["Mic L", "", "Manager Out", ""]
    plain, _n = BusConfig.from_decoder_config(d)
    assert [dp.name for dp in plain.dataports] == [""] * 12
    assert [dp.name for dp in BusConfig.from_decoder_config(d, names={})[0].dataports] == [""] * 12


def test_a_decoder_port_with_an_unset_number_takes_no_name():
    from swi3s_studio.model.bus_config import BusConfig
    d = {"dataports": [{"enabled": False, "device_number": 1, "dp_number": 0},
                       {"enabled": True, "device_number": 1, "dp_number": -1, "enable_ch": 3},
                       {"enabled": True, "device_number": 1, "dp_number": 4, "enable_ch": 3}]}
    names = {(1, 0): "Slot Zero", (1, 1): "Slot One", (1, 4): "Mic"}
    cfg, n = BusConfig.from_decoder_config(d, names=names)
    assert n == 2
    assert [dp.name for dp in cfg.dataports[:3]] == ["", "Mic", ""]


def test_view_bus_grid_in_visualizer_names_the_chips_and_the_unnamed_keep_their_default(win):
    _name_ports(win, {(0, 0): "AMP1", (0, 1): "AMP2", (0, 3): "MIC2"})
    win.cursor.set_sample(2_000_000)                 # past the demo's port configuration
    win.open_grid_in_visualizer()
    QApplication.processEvents()
    cfg = win._authoring.config()
    assert [dp.name for dp in cfg.dataports[:4]] == ["AMP1", "AMP2", "", "MIC2"]
    assert _chips(win)[:4] == ["AMP1", "AMP2", "DP2", "MIC2"]
    assert _chips(win)[4:] == [f"DP{c}" for c in range(4, 12)]


def test_a_name_carried_over_is_saved_and_loaded_like_one_typed_in_the_visualizer(
        win, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QFileDialog

    from swi3s_studio.model.bus_config import BusConfig
    _name_ports(win, {(0, 0): "AMP1", (0, 1): "AMP2", (0, 3): "MIC2"})
    win.cursor.set_sample(2_000_000)
    win.open_grid_in_visualizer()
    QApplication.processEvents()
    ctl = win._authoring_ctl
    path = str(tmp_path / "bus_config.csv")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, "CSV (*.csv)"))
    ctl.save_csv()
    named = _csv_rows(path)
    assert dict(named)["Name"] == ["AMP1", "AMP2", "DP2", "MIC2"] + [
        f"DP{c}" for c in range(4, 12)]
    # Only the Name row differs from the same grid opened with no names.
    for dp in win._session.dp_names.copy():
        win._session.set_dp_name(*dp, "")
    win.open_grid_in_visualizer()
    QApplication.processEvents()
    bare = str(tmp_path / "bare.csv")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (bare, "CSV (*.csv)"))
    ctl.save_csv()
    plain = _csv_rows(bare)
    assert [k for k, _v in named] == [k for k, _v in plain]
    assert [k for (k, v), (_k, w) in zip(named, plain) if v != w] == ["Name"]
    # Loaded back, the names are the config's and the chips'.
    win._authoring.set_config(BusConfig())
    ctl.open_csv_path(path)
    QApplication.processEvents()
    assert [dp.name for dp in win._authoring.config().dataports[:4]] == [
        "AMP1", "AMP2", "", "MIC2"]
    # A saved file names every port, the default "DP2" for an unnamed one; read back, that
    # port is unnamed again and its chip reads "DP2".
    assert _chips(win)[:4] == ["AMP1", "AMP2", "DP2", "MIC2"]


# ---- Export Visualizer CSV carries the names too ----

def _export_csv(win, monkeypatch, path):
    from PySide6.QtWidgets import QFileDialog
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, "CSV (*.csv)"))
    win.export_grid_csv()


def test_export_visualizer_csv_holds_the_names_and_the_unnamed_keep_their_default(
        win, monkeypatch, tmp_path):
    _name_ports(win, {(0, 0): "AMP1", (0, 1): "AMP2", (0, 3): "MIC2"})
    win.cursor.set_sample(2_000_000)
    out = str(tmp_path / "export.csv")
    _export_csv(win, monkeypatch, out)
    assert dict(_csv_rows(out))["Name"] == ["AMP1", "AMP2", "DP2", "MIC2"] + [
        f"DP{c}" for c in range(4, 12)]


def test_export_visualizer_csv_writes_the_name_row_save_settings_writes_after_view_bus_grid(
        win, monkeypatch, tmp_path):
    from PySide6.QtWidgets import QFileDialog
    _name_ports(win, {(0, 0): "AMP1", (0, 1): "AMP2", (0, 3): "MIC2"})
    win.cursor.set_sample(2_000_000)
    out = str(tmp_path / "export.csv")
    _export_csv(win, monkeypatch, out)
    win.open_grid_in_visualizer()
    QApplication.processEvents()
    saved = str(tmp_path / "saved.csv")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (saved, "CSV (*.csv)"))
    win._authoring_ctl.save_csv()
    assert dict(_csv_rows(out))["Name"] == dict(_csv_rows(saved))["Name"]
    assert dict(_csv_rows(out))["Name"][0] == "AMP1"


def test_export_visualizer_csv_of_an_unnamed_session_has_the_default_name_row(
        win, monkeypatch, tmp_path):
    win.cursor.set_sample(2_000_000)
    out = str(tmp_path / "export.csv")
    _export_csv(win, monkeypatch, out)
    assert dict(_csv_rows(out))["Name"] == [f"DP{c}" for c in range(12)]
