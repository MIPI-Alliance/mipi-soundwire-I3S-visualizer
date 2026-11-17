"""Registers pane: a context-menu action after a rebuild, the Compare overlay's tooltip, and
a long Fields list.

Pinned here:
- an action chosen from a row's context menu finds its row again by (device, address), so a
  rebuild between building the menu and choosing the action does not leave it holding a
  deleted row;
- a register the Compare overlay names shows the expected value, and for a register the
  bus wrote its tooltip names the value written (the read, reset, CSV-import and
  manual-edit tooltips are test_ui_pane_robustness.py's);
- a Fields cell wider than its column is elided and carries its full text as a tooltip;
- the device dropdown keeps its selection across a rename.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_register_view_fixes.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QTreeWidgetItemIterator

from swi3s_studio.model import DeviceRegisterFile, RegisterMap
from swi3s_studio.model.registers import SLC_BASE
from swi3s_studio.ui.register_view import RegisterView

pytestmark = pytest.mark.gui

_NUM_COLUMNS = SLC_BASE + 0x81


@pytest.fixture
def view():
    QApplication.instance() or QApplication([])
    rmap = RegisterMap.load()
    f = DeviceRegisterFile(rmap, 0)
    f.apply_write(_NUM_COLUMNS, bytes([0x05]))
    v = RegisterView(rmap)
    v.set_files({0: f})
    v.resize(900, 700)
    v.show()
    QApplication.processEvents()
    yield v, f
    v.close()
    v.deleteLater()


def _row(v, addr):
    it = QTreeWidgetItemIterator(v._tree)
    while it.value():
        key = it.value().data(0, Qt.UserRole)
        if key and int(key[1]) == addr and not it.value().text(0).endswith("(CURR)"):
            return it.value()
        it += 1
    raise AssertionError(hex(addr))


def test_an_action_chosen_after_a_rebuild_acts_on_the_register_now_in_the_tree(view):
    v, f = view
    asked = []
    v.historyRequested.connect(lambda d, a, label: asked.append((d, a)))
    menu = v._context_menu_for(_row(v, _NUM_COLUMNS))
    v.set_files({0: f})                    # a rebuild deletes every row the menu was built on
    history = next(a for a in menu.actions() if a.text() == "History…")
    history.trigger()
    assert asked == [(0, _NUM_COLUMNS)]


def test_an_action_for_a_register_no_longer_shown_does_nothing(view):
    v, f = view
    asked = []
    v.historyRequested.connect(lambda d, a, label: asked.append((d, a)))
    menu = v._context_menu_for(_row(v, _NUM_COLUMNS))
    v.set_files({})                        # the device is gone
    v._device.setCurrentIndex(-1)
    next(a for a in menu.actions() if a.text() == "History…").trigger()
    assert asked == []


def test_the_compare_overlay_shows_the_expected_value_and_tips_the_bus_value(view):
    v, f = view
    v.set_expected([(0, _NUM_COLUMNS, 0x07)])
    row = _row(v, _NUM_COLUMNS)
    assert row.text(2) == "0x07"
    assert "the bus wrote 0x05" in row.toolTip(0) and "expected" in row.toolTip(0)


def test_a_fields_list_wider_than_its_column_is_elided_and_carries_a_tooltip(view):
    v, _ = view
    assert v._tree.textElideMode() == Qt.ElideRight
    it = QTreeWidgetItemIterator(v._tree)
    longest = ""
    while it.value():
        item = it.value()
        if item.text(3):
            assert item.toolTip(3) == item.text(3)
            longest = max(longest, item.text(3), key=len)
        it += 1
    fm = v._tree.fontMetrics()
    assert fm.horizontalAdvance(longest) > v._tree.columnWidth(3), "no field list overflows"


def test_the_device_dropdown_keeps_its_selection_across_a_rename(view):
    v, f = view
    v.set_files({0: f, 1: DeviceRegisterFile(RegisterMap.load(), 1)})
    v._device.setCurrentIndex(v._device.findData(1))
    v.set_device_names({1: "Mic", 0: "Mgr"})
    assert v.current_device() == 1 and "Mic" in v._device.currentText()


def test_a_device_chosen_before_it_has_a_file_stays_chosen(view):
    """Moving the cursor to an instant before the chosen device has any activity keeps it
    in the dropdown and selected, and the choice is still there when its file returns."""
    v, f = view
    late = DeviceRegisterFile(RegisterMap.load(), 3)
    v.set_files({0: f, 3: late})
    v._device.setCurrentIndex(v._device.findData(3))
    assert v.current_device() == 3
    v.set_files({0: f})                              # an instant before device 3 is written
    assert v.current_device() == 3
    v.set_files({})
    assert v.current_device() == 3
    v.set_files({0: f, 3: late})
    assert v.current_device() == 3


def test_binding_another_link_forgets_the_chosen_device(view):
    v, f = view

    class _Sess:
        pass

    a, b = _Sess(), _Sess()
    v.bind_link(a)
    v.set_files({0: f, 3: DeviceRegisterFile(RegisterMap.load(), 3)})
    v._device.setCurrentIndex(v._device.findData(3))
    v.bind_link(a)                                   # the same Link again: kept
    assert v._chosen_device == 3
    v.bind_link(b)
    v.set_files({0: f})
    assert v.current_device() == 0


def test_a_saved_device_with_no_file_at_the_cursor_is_chosen_and_kept(view):
    from swi3s_studio.ui import view_state
    v, f = view
    late = DeviceRegisterFile(RegisterMap.load(), 3)
    assert v._device.findData(3) < 0                      # nothing written to device 3 yet
    view_state.restore_registers(v, {"device": 3})
    assert v.current_device() == 3
    v.set_files({0: f})                                   # a scrub keeps the restored pick
    assert v.current_device() == 3
    v.set_files({0: f, 3: late})
    assert v.current_device() == 3


def test_a_saved_device_that_is_already_current_is_still_a_pick(view):
    from swi3s_studio.ui import view_state
    v, f = view
    assert v.current_device() == 0 and v._chosen_device is None
    view_state.restore_registers(v, {"device": 0})
    assert v._chosen_device == 0
    v.set_files({})
    assert v.current_device() == 0


@pytest.mark.parametrize("saved", [12, -1, 10 ** 9, 1.5, "3", True])
def test_a_saved_device_outside_the_device_range_is_reported(view, saved):
    from swi3s_studio import import_notes
    from swi3s_studio.ui import view_state
    v, f = view
    with import_notes.collect() as notes:
        view_state.restore_registers(v, {"device": saved}, source="ws.json")
    assert len(notes) == 1 and "device" in notes[0].message
    assert v.current_device() == 0
