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


def test_the_grid_key_widens_to_fit_a_name(win):
    """A swatch is as wide as its label, and no narrower than the 46 px the numbers had,
    and the swatches do not overlap."""
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
    item = next(i for i in ev._filter_items if i.get("port") == (0, 1))
    item["action"].setChecked(True)
    _named(win, monkeypatch)
    assert item["action"].text() == "Mic L · Device 0 DP1" and item["action"].isChecked()
    # A rebuild (a re-decode, a reopened workspace) labels it the same way, so a filter
    # saved by its labels is found again.
    win.load_session(win._session)
    QApplication.processEvents()
    labels = [i["action"].text() for i in win._eye_view._filter_items if i.get("port")]
    assert "Mic L · Device 0 DP1" in labels
