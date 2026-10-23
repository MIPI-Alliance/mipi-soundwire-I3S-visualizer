"""Widgets sized from the platform's own metrics, not from one platform's pixels.

What the gate's screenshots check found on another platform's style, held here on every one:
the Timing Calculator's buttonless spin boxes keep an edit field wide enough for their value,
the Bus Visualizer's buttons are at least as wide as their labels, and bus grid text is centred
vertically on its capitals.
"""
from PySide6.QtCore import Qt
from PySide6.QtGui import QFontMetricsF
from PySide6.QtWidgets import QApplication, QPushButton

from swi3s_studio.ui import timing_view
from swi3s_studio.ui.authoring.authoring_panel import AuthoringPanel
from swi3s_studio.ui.grid_view import GridView

_app = QApplication.instance() or QApplication([])


def _settle():
    for _ in range(5):
        _app.processEvents()


def test_a_buttonless_spin_box_keeps_room_for_its_value():
    """The buttons' rule anchors them to the border; without it a styled spin box on Windows
    lays its edit field out 1 px wide. The edit field fills the box but for its frame and
    padding, on the page's own Clock Frequency Target box. Whether the text then fits is a
    question of the platform's font, which the gate's screenshots check answers on each
    platform's own style."""
    assert "subcontrol-origin: border" in timing_view._spin_qss()
    page = timing_view.TimingView()
    page.setAttribute(Qt.WA_DontShowOnScreen, True)
    page.resize(1200, 800)
    page.show()
    _settle()
    box = page._fclk
    edit = box.lineEdit()
    assert edit.width() >= box.width() - 16, (edit.geometry(), box.geometry())
    page.close()


def test_the_visualizer_buttons_are_as_wide_as_their_labels():
    panel = AuthoringPanel()
    buttons = [b for b in panel.findChildren(QPushButton) if b.text()]
    assert buttons
    for b in buttons:
        assert b.width() >= b.sizeHint().width() or b.minimumWidth() >= b.sizeHint().width(), \
            (b.text(), b.minimumWidth(), b.sizeHint().width())
    action = panel._action_btns
    assert len({b.minimumWidth() for b in action}) == 1, "one width for the button column"


def test_grid_text_is_centred_on_its_capitals():
    view = GridView()
    before = set(view._scene.items())
    view._text("C4B15", 0, 100, 60, 24, Qt.black)
    item = next(i for i in view._scene.items() if i not in before)
    fm = QFontMetricsF(view._f_label)
    middle = item.pos().y() + fm.ascent() - fm.capHeight() / 2
    assert abs(middle - (100 + 24 / 2)) < 0.01
