"""Naming a config region: Timeline ▸ right-click a region band ▸ Rename…. The name is the
Link's (Session.region_names, in its source descriptor, so a workspace keeps it), keyed by
section index like a region column pin, and the band shows it before what it is."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QApplication, QInputDialog, QMenu

from swi3s_studio.session import Session


def test_a_region_name_is_kept_in_the_source_and_survives_a_reopen():
    s = Session.from_demo(300, phy=2, cold_start=True)
    s.set_region_name(1, "  Playback ")
    assert s.region_names == {1: "Playback"} and s.source["region_names"] == {"1": "Playback"}
    again = Session.from_source(dict(s.source))
    assert again.region_names == {1: "Playback"}
    again.set_region_name(1, "")
    assert again.region_names == {} and "region_names" not in again.source


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


def _band_point(ribbon, section):
    """A widget point on the band strip inside `section`'s band."""
    from swi3s_studio.ui import timeline
    segs = ribbon._segments
    lo = int(segs[section]["start_sample"])
    hi = int(segs[section + 1]["start_sample"]) if section + 1 < len(segs) else ribbon._total
    x = ribbon._x_for((lo + hi) // 2)
    return QPointF(x, ribbon._track_rect().top() + timeline._CFG_BAND_H / 2)


def _press(ribbon, pt, button):
    ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress, pt, ribbon.mapToGlobal(pt.toPoint()),
                     button, button, Qt.NoModifier)
    ribbon.mousePressEvent(ev)


def test_right_click_on_a_band_asks_for_its_menu_and_does_not_seek(win, monkeypatch):
    ribbon = win._timeline
    asked, seeks, menus = [], [], []
    monkeypatch.setattr(QMenu, "exec", lambda self, *_a: menus.append(
        [a.text() for a in self.actions()]))            # the window's menu, dismissed
    ribbon.regionMenuRequested.connect(lambda sect, pos: asked.append(sect))
    ribbon.seeked.connect(seeks.append)
    _press(ribbon, _band_point(ribbon, 1), Qt.RightButton)
    assert asked == [1] and seeks == [] and menus == [["Rename…"]]
    below = QPointF(_band_point(ribbon, 1).x(), ribbon.height() - 4)   # under the strip
    _press(ribbon, below, Qt.RightButton)
    assert asked == [1] and len(seeks) == 1                            # a seek, as before


def test_rename_names_the_band_and_keeps_the_zoom(win, monkeypatch):
    ribbon = win._timeline
    lo, hi = ribbon._view_lo, ribbon._view_lo + ribbon._view_span()
    monkeypatch.setattr(QMenu, "exec", lambda self, *_a: next(
        a for a in self.actions() if a.text() == "Rename…"))
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("Playback", True)))
    win._region_band_menu(ribbon, 1, QPoint(0, 0))
    assert win._session.region_names == {1: "Playback"}
    seg = ribbon._segments[1]
    assert ribbon._band_label(1, seg).startswith("Playback · ")
    assert not ribbon._band_label(0, ribbon._segments[0]).startswith("Playback")
    assert (ribbon._view_lo, ribbon._view_lo + ribbon._view_span()) == (lo, hi)
    monkeypatch.setattr(QInputDialog, "getText", staticmethod(lambda *a, **k: ("", True)))
    win._region_band_menu(ribbon, 1, QPoint(0, 0))
    assert win._session.region_names == {} and "name" not in ribbon._segments[1]


def test_a_re_decode_keeps_the_name(win):
    win._session.set_region_name(2, "Late")
    win.load_session(win._session)
    QApplication.processEvents()
    assert win._timeline._segments[2].get("name") == "Late"


def test_a_workspace_keeps_the_name(win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QFileDialog

    from swi3s_studio.workspace import Workspace
    win._session.set_region_name(1, "Playback")
    path = str(tmp_path / "w.swi3s")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (path, ""))
    win.save_workspace()
    assert Workspace.load(path).links[0].source["region_names"] == {"1": "Playback"}


def test_right_click_on_the_bring_up_is_not_region_0s(win, monkeypatch):
    """Region 0 starts before the bring-up ends, and the bring-up's bands are drawn over
    that stretch; a right-click there seeks as before rather than offering to rename it."""
    ribbon = win._timeline
    b = ribbon._bringup
    assert b and int(b.get("audio_start", 0)) > int(ribbon._segments[0]["start_sample"])
    from swi3s_studio.ui import timeline
    x = ribbon._x_for(int(b["audio_start"]) // 2)
    pt = QPointF(x, ribbon._track_rect().top() + timeline._CFG_BAND_H / 2)
    asked, seeks = [], []
    ribbon.regionMenuRequested.connect(lambda sect, pos: asked.append(sect))
    ribbon.seeked.connect(seeks.append)
    _press(ribbon, pt, Qt.RightButton)
    assert asked == [] and len(seeks) == 1
