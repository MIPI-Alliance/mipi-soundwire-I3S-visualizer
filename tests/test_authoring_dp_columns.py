"""The Bus Visualizer's data port columns follow their name chips: a column is as wide as its
chip's label ("Left Microphone Array") needs, from the default width up to a maximum past
which the label is elided and the tooltip holds it whole. A rename widens the column, a
shorter one narrows it again, and a loaded config's names size the columns as it opens. The
column's entries take the chip's width. A chip opens its Number & Name dialog on the release
of a click on it, and a rename through it leaves the panel taking clicks. Offscreen Qt."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

pytestmark = pytest.mark.gui

LONG = "Left Microphone Array"


@pytest.fixture
def panel():
    QApplication.instance() or QApplication([])
    from swi3s_studio.model.bus_config import demo_config
    from swi3s_studio.ui.authoring.authoring_panel import AuthoringPanel
    p = AuthoringPanel()
    p.resize(1700, 640)
    p.show()
    p.set_config(demo_config())
    QApplication.processEvents()
    yield p
    p.close()


def _rename(panel, monkeypatch, c, name):
    """Rename data port `c` through its chip's Number & Name dialog, accepted."""
    from swi3s_studio.ui.authoring import authoring_panel as ap

    class _Dialog:
        def __init__(self, _parent, number, _name, _c):
            self.dp_number, self.name = number, name

        def exec(self):
            return True

    monkeypatch.setattr(ap, "DataPortIdentityDialog", _Dialog)
    panel._open_dp_dialog("identity", c)
    QApplication.processEvents()


def _shows_whole(chip) -> bool:
    """Whether the chip draws its whole text unscrolled: the caret's travel from the first
    character to past the last is the text's width, and both ends are inside the chip. A text
    wider than the chip scrolls as the caret moves, so its travel is shorter."""
    text = chip.text()
    chip.setCursorPosition(0)
    start = chip.cursorRect()
    chip.setCursorPosition(len(text))
    end = chip.cursorRect()
    travel = end.center().x() - start.center().x()
    return (abs(travel - chip.fontMetrics().horizontalAdvance(text)) <= 1
            and chip.rect().contains(start) and chip.rect().contains(end))


def _column_widths(panel, c):
    return {panel._dp_name[c].width()} | {w[c].width() for w in panel._dp_num.values()}


def test_every_default_chip_shows_its_label_at_the_default_width(panel):
    from swi3s_studio.ui.authoring.authoring_panel import _ENTRY_W
    for c, chip in enumerate(panel._dp_name):
        assert chip.text() == f"DP{c}" and _shows_whole(chip), chip.text()
        assert _column_widths(panel, c) == {_ENTRY_W}


def test_a_long_rename_widens_its_column_to_show_the_name(panel, monkeypatch):
    from swi3s_studio.ui.authoring.authoring_panel import _DP_COL_MAX, _ENTRY_W
    _rename(panel, monkeypatch, 1, LONG)
    chip = panel._dp_name[1]
    assert chip.text() == LONG and _shows_whole(chip)
    (width,) = _column_widths(panel, 1)                 # chip and entries share one width
    assert _ENTRY_W < width <= _DP_COL_MAX
    assert all(_column_widths(panel, c) == {_ENTRY_W} for c in range(12) if c != 1)


def test_a_shorter_rename_narrows_the_column_again(panel, monkeypatch):
    from swi3s_studio.ui.authoring.authoring_panel import _ENTRY_W
    _rename(panel, monkeypatch, 1, LONG)
    wide = panel._dp_name[1].width()
    _rename(panel, monkeypatch, 1, "Mic L")
    chip = panel._dp_name[1]
    assert chip.text() == "Mic L" and _shows_whole(chip)
    assert _ENTRY_W <= chip.width() < wide
    _rename(panel, monkeypatch, 1, "")                  # back to the number
    assert panel._dp_name[1].text() == "DP1" and _column_widths(panel, 1) == {_ENTRY_W}


def test_a_loaded_config_sizes_its_named_columns(panel):
    from swi3s_studio.model.bus_config import demo_config
    from swi3s_studio.ui.authoring.authoring_panel import _ENTRY_W
    cfg = demo_config()
    cfg.dataports[3].name = LONG
    panel.set_config(cfg)
    QApplication.processEvents()
    assert panel._dp_name[3].text() == LONG and _shows_whole(panel._dp_name[3])
    assert panel._dp_name[3].width() > _ENTRY_W


def test_a_name_past_the_maximum_is_elided_with_the_whole_name_in_the_tooltip(panel, monkeypatch):
    from swi3s_studio.ui.authoring.authoring_panel import _DP_COL_MAX
    name = "Rear Left Surround Microphone Array Channel Pair"
    _rename(panel, monkeypatch, 2, name)
    chip = panel._dp_name[2]
    assert _column_widths(panel, 2) == {_DP_COL_MAX}
    assert chip.text().endswith("…") and chip.text() != name and _shows_whole(chip)
    assert name in chip.toolTip()


def _nested_dialog(monkeypatch, name):
    """Replace the chip's Number & Name dialog with one that runs its own modal event loop,
    as QDialog.exec does, and accepts `name` from inside that loop. Returns the list the
    dialog appends to each time it opens."""
    from PySide6.QtCore import QEventLoop, Qt, QTimer
    from PySide6.QtWidgets import QDialog

    from swi3s_studio.ui.authoring import authoring_panel as ap
    opened = []

    class _Dialog(QDialog):
        def __init__(self, parent, number, _name, _c):
            super().__init__(parent)
            self.dp_number, self.name = number, _name

        def exec(self):
            opened.append(1)
            self.setWindowModality(Qt.ApplicationModal)
            self.show()
            loop = QEventLoop()
            self.finished.connect(loop.quit)
            QTimer.singleShot(50, self._take)
            loop.exec()
            return self.result()

        def _take(self):
            self.name = name
            self.accept()

    monkeypatch.setattr(ap, "DataPortIdentityDialog", _Dialog)
    return opened


def test_a_chip_opens_its_dialog_on_the_release_of_a_click_on_it(panel, monkeypatch):
    """A chip emits `clicked` when a left press on it is released on it, never on the press
    alone, and not for a press released off it: the dialog it opens follows the whole click."""
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    chip = panel._dp_name[0]
    got = _nested_dialog(monkeypatch, "")
    inside = chip.rect().center()
    QTest.mousePress(chip, Qt.LeftButton, Qt.NoModifier, inside)
    assert got == []
    QTest.mouseRelease(chip, Qt.LeftButton, Qt.NoModifier, inside)
    assert got == [1]
    QTest.mousePress(chip, Qt.LeftButton, Qt.NoModifier, inside)
    QTest.mouseRelease(chip, Qt.LeftButton, Qt.NoModifier, QPoint(-5, -5))
    assert got == [1]
    assert not chip.hasSelectedText()


def test_after_a_rename_through_the_chip_the_checkboxes_and_buttons_take_clicks(panel,
                                                                                monkeypatch):
    """A rename through the chip, its dialog running a modal loop of its own and accepted
    from inside it, leaves the panel taking clicks: a checkbox ticks and Maximize Frame
    emits."""
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QPushButton
    opened = _nested_dialog(monkeypatch, "DP0-MIC")
    QTest.mouseClick(panel._dp_name[0], Qt.LeftButton)
    QApplication.processEvents()
    assert opened == [1] and panel._dp_name[0].text() == "DP0-MIC"
    cb = panel._dp_check["direction"][0]
    before = cb.isChecked()
    QTest.mouseClick(cb, Qt.LeftButton)
    assert cb.isChecked() != before and opened == [1]
    got = []
    panel.maximizeRequested.connect(lambda: got.append(1))
    QTest.mouseClick(next(b for b in panel.findChildren(QPushButton)
                          if b.text() == "Maximize Frame"), Qt.LeftButton)
    assert got == [1]
