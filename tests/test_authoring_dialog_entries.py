"""Authoring dialogs: number entries and values the dialogs are handed.

Pinned here:
- the Flow Control dialog commits only entries inside their ranges: OK with an entry out of
  range, while the parameters apply (Rx, Async), leaves the dialog open with the entry
  marked and the attributes unchanged; a thousands separator cannot be typed;
- with the parameters not applying (Normal, Tx) an out-of-range entry is not committed and
  does not block OK;
- the CDS Settings dialog opens for a bit width outside 0-7, with no bit-width button
  selected, and the value stays until a button is picked.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_authoring_dialog_entries.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication, QDialog

from swi3s_studio.ui.authoring import dialogs as D

pytestmark = pytest.mark.gui


@pytest.fixture(scope="module", autouse=True)
def _app():
    return QApplication.instance() or QApplication([])


def _flow(mode: int):
    dlg = D.FlowModeSelectorDialog(None, mode, 0, fcp_h_start=4, fcp_offset=7)
    return dlg


@pytest.mark.parametrize("mode", [2, 3])
def test_an_entry_out_of_range_keeps_the_flow_dialog_open_and_uncommitted(mode):
    dlg = _flow(mode)
    dlg._hs.setText("40")
    dlg._off.setText("12")
    dlg.accept()
    assert dlg.result() != QDialog.Accepted
    assert (dlg.fcp_h_start, dlg.fcp_offset) == (4, 7)
    assert "border" in dlg._hs.styleSheet() and dlg._off.styleSheet() == ""
    dlg._hs.setText("30")                     # editing clears the mark
    assert dlg._hs.styleSheet() == ""
    dlg.accept()
    assert dlg.result() == QDialog.Accepted
    assert (dlg.fcp_h_start, dlg.fcp_offset) == (30, 12)


def test_a_thousands_separator_cannot_be_typed_in_an_entry():
    dlg = _flow(2)
    state, _t, _p = dlg._off.validator().validate("1,000", 5)
    assert state != dlg._off.validator().State.Acceptable
    dlg._off.setText("1,000")
    dlg.accept()                              # no exception; the entry is refused
    assert dlg.result() != QDialog.Accepted and dlg.fcp_offset == 7


def test_an_empty_entry_is_zero():
    dlg = _flow(3)
    dlg._off.setText("")
    dlg.accept()
    assert dlg.result() == QDialog.Accepted and dlg.fcp_offset == 0


@pytest.mark.parametrize("mode", [0, 1])
def test_an_entry_out_of_range_is_ignored_where_the_parameters_do_not_apply(mode):
    dlg = _flow(mode)
    dlg._hs.setText("40")
    dlg.accept()
    assert dlg.result() == QDialog.Accepted and dlg.fcp_h_start == 4


@pytest.mark.parametrize("width", [9, -1, 8])
def test_cds_settings_opens_for_a_bit_width_outside_the_registers_range(width):
    from swi3s_studio.model.bus_config import CDS_DRIVE_NORMAL
    dlg = D.CdsSettingsDialog(
        None, bit_width=width, drive_type=[CDS_DRIVE_NORMAL] * 13, end_drive_early=[0] * 13,
        enforce_handover=True, guard=[0] * 13, tail=[0] * 13)
    assert dlg.bit_width == width
    assert all(btn.styleSheet() == D._UNSEL_CSS for btn in dlg._bit.values())
    dlg._pick_bit(3)
    assert dlg.bit_width == 3 and dlg._bit[3].styleSheet() == D._SEL_CSS
