"""Pane chrome from the 3.0.19 review: what the axes, pickers and band labels show.

- Capture and Audio carry no axis titles ("Time", "Amplitude"): the ticks carry the unit,
  and the room goes to the traces.
- Audio's Y axis labels -1, 0 and +1 at full scale, and only the data's two extremes and 0
  when zoomed, in plain values. Its end labels used to be culled (pyqtgraph keeps an
  overflowing label only on an axis WITHOUT a grid), which left just "0".
- Every Link picker draws the same chevron, the Commands one and those in the dock title
  bars; it is in the shipped assets.
- The timeline's Link-name column is as narrow as the names allow.
- The waveform colours and line weight are reachable from the View menu.
- In All Links, lanes of different lengths share one window and put their ticks at the
  same global times.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pathlib

import pytest
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication, QCheckBox
from test_links_gui import _win, _with_added_link

from swi3s_studio.ui.audio_view import _amp_ticks


def _labels(axis):
    """The tick labels the axis would draw, once it has widened to fit them (pyqtgraph
    grows an axis after measuring its text, on the next layout pass)."""
    for _ in range(2):
        img = QImage(200, 200, QImage.Format_ARGB32)
        p = QPainter(img)
        try:
            out = [t[2] for t in axis.generateDrawSpecs(p)[2]]
        finally:
            p.end()
        QApplication.processEvents()
    return out


@pytest.fixture
def win():
    w = _win()
    w.resize(1500, 1000)
    w.show()
    w._mode_mgr.switch_to("Analysis")
    w._audio_dock.show()
    w._audio_dock.raise_()
    QApplication.processEvents()
    return w


def test_capture_and_audio_have_no_axis_titles(win):
    plot = win._audio_view._plots[0][0]
    for side in ("bottom", "left"):
        assert not plot.getAxis(side).labelText, side
    assert not win._raw_view._plot.getAxis("bottom").labelText


def test_amp_ticks_full_scale_and_zoomed():
    assert _amp_ticks(-1.0, 1.0) == [[(-1.0, "-1"), (0.0, "0"), (1.0, "+1")]]
    assert _amp_ticks(-0.0004, 0.00022) == [[(-0.0004, "-0.0004"), (0.0, "0"),
                                             (0.00022, "+0.00022")]]
    assert _amp_ticks(0.1, 0.45) == [[(0.1, "+0.1"), (0.45, "+0.45")]]   # 0 out of range


def test_the_full_scale_labels_survive_with_the_grid_on(win):
    axis = win._audio_view._plots[0][0].getAxis("left")
    assert axis.grid is not False                     # the case that culled them
    assert _labels(axis) == ["-1", "0", "+1"]


def test_vertical_zoom_labels_the_extremes_only(win):
    av = win._audio_view
    av._vzoom_btn.click()
    try:
        plot = av._plots[0][0]
        labels = _labels(plot.getAxis("left"))
        assert 2 <= len(labels) <= 3 and not any("x0." in t for t in labels), labels
        lo, hi = plot.getAxis("left")._tickLevels[0][0][0], plot.getAxis("left")._tickLevels[0][-1][0]
        y0, y1 = plot.getViewBox().viewRange()[1]
        assert y0 <= lo < hi <= y1
    finally:
        av._vzoom_btn.click()


def test_a_new_store_leaves_no_old_checkboxes_on_screen(win):
    av = win._audio_view
    win.set_group_link("bottom", 0)                   # same store: rebuild the list
    win._bind_link(0, filters="keep", feed_timeline=False)
    shown = [cb for cb in av._chan_box.parentWidget().findChildren(QCheckBox)]
    assert len(shown) == len(av._checks)


def test_every_link_picker_has_the_chevron():
    from swi3s_studio.ui import theme
    win, _first, _second = _with_added_link()
    pickers = [p for ps in win._pane_pickers.values() for p in ps]
    assert pickers
    for picker in pickers:
        assert "::down-arrow" in picker.styleSheet() and "chevron-down-" in picker.styleSheet()
    assert "::down-arrow" in theme.analyzer_stylesheet()      # the Commands picker's
    for mode in ("dark", "light"):
        assert (pathlib.Path(theme._ASSETS) / f"chevron-down-{mode}.svg").is_file()
    spec = (pathlib.Path(__file__).resolve().parent.parent / "swi3s-studio.spec").read_text()
    assert '"swi3s_studio", "ui", "assets"' in spec          # shipped with the app


def test_the_band_labels_are_as_narrow_as_the_names_allow():
    from swi3s_studio.ui.main_window import _LinkBandLabel
    win, _first, _second = _with_added_link()
    short = win._ribbon_labels[0].width()
    assert short < 96 and len({lbl.width() for lbl in win._ribbon_labels}) == 1
    win._links.rename(1, "A rather long Link name for this bus")
    win._sync_timeline_rows()
    assert win._ribbon_labels[0].width() == _LinkBandLabel.MAX_WIDTH


def test_the_view_menu_opens_the_waveform_preferences(win, monkeypatch):
    """Preferences is in macOS's application menu (Settings…), where it was not found;
    View names it for what it holds."""
    from swi3s_studio.ui.preferences_dialog import PreferencesDialog
    opened = []
    monkeypatch.setattr(PreferencesDialog, "exec", lambda self: opened.append(self) or 0)
    act = next(a for a in win._view_menu.actions()
               if a.text().replace("&", "").startswith("Waveform Colors"))
    act.trigger()
    assert len(opened) == 1
