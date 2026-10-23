"""Tabbed panes show their group's tabs in the title bar.

The right group (Registers, Statistics, Cursors) and the bottom group (Bus Grid, Capture,
CDS, Audio, Samples, Timing) each draw their tabs in the title bar of the pane in front, where
the title was; the dock area's own tab bar is collapsed to no height. A click on a tab brings
that pane to the front, and a pane alone in its area shows its title.
"""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTabBar

from swi3s_studio.ui.main_window import MainWindow

_app = QApplication.instance() or QApplication([])

_RIGHT = ["Registers", "Statistics", "Cursors"]
_BOTTOM = ["Bus Grid", "Capture", "CDS", "Audio", "Samples", "Timing"]


def _settle():
    for _ in range(6):
        _app.processEvents()


@pytest.fixture
def win():
    w = MainWindow()
    w.resize(1400, 900)
    w.show()
    w.load_demo()
    _settle()
    yield w
    w.close()
    _settle()


def _bar(dock):
    return dock.titleBarWidget()


def test_each_tabbed_pane_shows_its_groups_tabs_in_order(win):
    for dock in (win._reg_dock, win._meas_dock, win._pair_dock):
        assert _bar(dock).tab_titles() == _RIGHT
    for dock in (win._grid_dock, win._raw_dock, win._audio_dock, win._eye_dock):
        assert _bar(dock).tab_titles() == _BOTTOM
    front = _bar(win._reg_dock)._tabs
    assert front.tabText(front.currentIndex()) == "Registers"
    assert _bar(win._reg_dock)._label.isHidden()


def test_the_dock_areas_own_tab_bars_take_no_height(win):
    own = win.findChildren(QTabBar, options=Qt.FindDirectChildrenOnly)
    assert own, "the dock area keeps a tab bar per group"
    for tb in own:
        assert tb.sizeHint().height() == 0 or tb.height() == 0, tb.sizeHint()


def test_a_click_on_a_tab_brings_its_pane_to_the_front(win):
    bar = _bar(win._reg_dock)
    bar._on_tab_clicked(_RIGHT.index("Cursors"))
    _settle()
    # Every tab of a group reports isVisible(); the one in front is the one showing.
    assert not win._pair_dock.visibleRegion().isEmpty()
    assert win._reg_dock.visibleRegion().isEmpty()
    tabs = _bar(win._pair_dock)._tabs
    assert tabs.tabText(tabs.currentIndex()) == "Cursors"


def test_a_closed_pane_leaves_the_tabs_and_a_pane_alone_shows_its_title(win):
    win._hide_dock(win._pair_dock)
    _settle()
    assert _bar(win._reg_dock).tab_titles() == ["Registers", "Statistics"]
    win._hide_dock(win._meas_dock)
    _settle()
    bar = _bar(win._reg_dock)
    assert bar.tab_titles() == []
    assert not bar._label.isHidden()
    win._show_dock(win._pair_dock)
    _settle()
    assert _bar(win._reg_dock).tab_titles() == ["Registers", "Cursors"]
