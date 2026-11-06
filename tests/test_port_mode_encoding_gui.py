"""DPn_PortMode in the widgets: the Bus Grid and the Port Test Mode dialog.

- the Bus Grid draws every channel bit of a PortMode 2 port as `T0` and of a PortMode 3 port
  as `T1`, for cells from the native core and from the swviz engine alike, and a Normal port's
  bits by weight;
- authoring: the Port Test Mode dialog writes 2 for Test0 and 3 for Test1, reopens with the
  stored mode highlighted, and shows a stored Reserved (1) as Normal.

The encoding itself is pinned in `test_port_mode_encoding.py`.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_port_mode_encoding_gui.py
"""
import pytest
from test_port_mode_encoding import _RAW_BIT, _data, _native_cells, _swviz_cells

pytestmark = pytest.mark.gui


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([])


@pytest.mark.parametrize("engine", ("native", "swviz"))
@pytest.mark.parametrize("port_mode", (2, 3))
def test_grid_draws_every_channel_bit_at_the_test_mode_level(qapp, engine, port_mode):
    """PortMode 2 draws each channel bit as `T0` and PortMode 3 as `T1`, in the Bit field."""
    from swi3s_studio.ui.grid_view import GridView
    cells = _native_cells(port_mode) if engine == "native" else _swviz_cells(port_mode)
    data = _data(cells)
    assert data
    want = f"T{_RAW_BIT[port_mode]}"
    for c in data:
        assert GridView._data_label(c, 4, 1) == want, c
        assert GridView._data_label(c, 6, 1) == f"C{c['channel']}{want}", c


def test_grid_draws_normal_mode_bits_by_weight(qapp):
    from swi3s_studio.ui.grid_view import GridView
    for c in _data(_native_cells(0)):
        assert GridView._data_label(c, 4, 1) == f"B{c['bit']}", c


@pytest.mark.parametrize("label, mode", [("Normal", 0), ("Test0", 2), ("Test1", 3)])
def test_authoring_dialog_writes_the_spec_value(qapp, label, mode):
    """The click changes the value: the dialog opens on a different mode."""
    from swi3s_studio.ui.authoring.dialogs import PortModeSelectorDialog
    dlg = PortModeSelectorDialog(None, 3 if mode != 3 else 2, 0)
    assert dlg.port_mode != mode
    btn = next(b for m, b in dlg._btns.items() if b.text().startswith(label))
    btn.click()
    dlg.accept()
    assert dlg.port_mode == mode


@pytest.mark.parametrize("stored, shown", [(0, 0), (1, 0), (2, 2), (3, 3)])
def test_authoring_dialog_reopens_with_the_stored_mode_highlighted(qapp, stored, shown):
    """A stored mode highlights its own button and no other; Reserved (1) opens as Normal."""
    from swi3s_studio.ui.authoring import dialogs
    dlg = dialogs.PortModeSelectorDialog(None, stored, 0)
    assert dlg.port_mode == shown
    assert dlg._btns[shown].text().startswith({0: "Normal", 2: "Test0", 3: "Test1"}[shown])
    for mode, btn in dlg._btns.items():
        assert btn.styleSheet() == (dialogs._SEL_CSS if mode == shown else dialogs._UNSEL_CSS)
