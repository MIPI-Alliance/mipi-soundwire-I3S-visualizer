"""Bus Grid merge rule and cell labels.

Pinned here:
- two adjacent cells merge only when their slot, data port and device match; past that a
  guard, tail, S0/S1 or handover ignores channel, sample and bit, and a data-ish cell (data,
  TxPresent, DRQ) also needs the same channel and sample, and the same bit when the Bit field
  is shown;
- a clash X over a CDS column widened for its flags line spans that column's width;
- a data cell's label takes the Sample / Channel / Bit fields it is set to show, the Bit
  field reading T0 for a port in PortMode 2 (Test0) and T1 in PortMode 3 (Test1), and a merged run of N cells adds
  " xN" only when the Bit field is shown;
- a merged S0 / S1 run reads "S0 xN" / "S1 xN".

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_grid_view_merge.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from swi3s_studio.ui import grid_view
from swi3s_studio.ui.grid_view import GridView

pytestmark = pytest.mark.gui

SAMPLE, CHANNEL, BIT = 1, 2, 4
_DATA, _TXP, _DRQ = grid_view._DATA, grid_view._TXP, grid_view._DRQ


def _cell(**kw):
    cell = {"slot": _DATA, "dp": 1, "device": 2, "channel": 0, "sample": 3, "bit": 5,
            "col": 4, "port_mode": 0}
    cell.update(kw)
    return cell


@pytest.mark.parametrize("key, other", [("slot", _TXP), ("dp", 2), ("device", 3)])
@pytest.mark.parametrize("df", [CHANNEL, CHANNEL | BIT])
def test_cells_of_another_slot_port_or_device_do_not_merge(key, other, df):
    assert GridView._can_merge(_cell(), _cell(), df)
    assert not GridView._can_merge(_cell(), _cell(**{key: other}), df)


@pytest.mark.parametrize("slot", [_DATA, _TXP, _DRQ])
@pytest.mark.parametrize("key", ["channel", "sample"])
def test_data_cells_of_another_channel_or_sample_do_not_merge(key, slot):
    for df in (CHANNEL, CHANNEL | BIT, SAMPLE):
        assert GridView._can_merge(_cell(slot=slot), _cell(slot=slot), df)
        assert not GridView._can_merge(_cell(slot=slot), _cell(slot=slot, **{key: 9}), df)


@pytest.mark.parametrize("slot", [_DATA, _TXP, _DRQ])
def test_data_cells_of_another_bit_merge_only_when_the_bit_field_is_hidden(slot):
    a, b = _cell(slot=slot, bit=5), _cell(slot=slot, bit=4)
    for df in (0, SAMPLE, CHANNEL, SAMPLE | CHANNEL):
        assert GridView._can_merge(a, b, df), df
    for df in (BIT, CHANNEL | BIT, SAMPLE | CHANNEL | BIT):
        assert not GridView._can_merge(a, b, df), df


@pytest.mark.parametrize("slot", [grid_view._G0, grid_view._G1, grid_view._TAIL,
                                  grid_view._S0, grid_view._S1, grid_view._HANDOVER])
def test_system_slots_ignore_channel_sample_and_bit_but_need_the_same_slot_port_and_device(
        slot):
    a = _cell(slot=slot, channel=0, sample=0, bit=0)
    b = _cell(slot=slot, channel=1, sample=7, bit=3)
    assert GridView._can_merge(a, b, CHANNEL | BIT)
    assert not GridView._can_merge(a, _cell(slot=slot, device=9), CHANNEL | BIT)
    assert not GridView._can_merge(a, _cell(slot=slot, dp=7), CHANNEL | BIT)


@pytest.mark.parametrize("df, n, mode, label", [
    (CHANNEL | BIT, 1, 0, "C0B5"), (CHANNEL | BIT, 3, 0, "C0B5 x3"),
    (SAMPLE | CHANNEL | BIT, 2, 0, "S3C0B5 x2"), (CHANNEL, 3, 0, "C0"),
    (SAMPLE, 4, 0, "S3"), (SAMPLE | CHANNEL, 2, 0, "S3C0"),
    (CHANNEL | BIT, 1, 2, "C0T0"), (CHANNEL | BIT, 1, 3, "C0T1"),
    (CHANNEL | BIT, 2, 2, "C0T0 x2"), (CHANNEL, 2, 3, "C0"), (BIT, 1, 1, "B5")])
def test_a_data_label_shows_its_fields_the_test_mode_bit_and_a_wide_bit_count(df, n, mode,
                                                                               label):
    assert GridView._data_label(_cell(port_mode=mode), df, n) == label


def test_tx_present_and_drq_labels_are_fixed():
    assert GridView._data_label(_cell(slot=_TXP, channel=1), CHANNEL | BIT, 3) == "TxP1"
    assert GridView._data_label(_cell(slot=_DRQ), CHANNEL | BIT, 3) == "DRQ"


@pytest.mark.parametrize("slot, n, label", [
    (grid_view._S0, 1, "S0"), (grid_view._S0, 3, "S0 x3"),
    (grid_view._S1, 1, "S1"), (grid_view._S1, 2, "S1 x2")])
def test_a_merged_sync_run_reads_its_count(monkeypatch, slot, n, label):
    QApplication.instance() or QApplication([])
    view = GridView()
    try:
        drawn = []
        monkeypatch.setattr(view, "_full_cell",
                            lambda r, c, extra, bg, text, *a, **k: drawn.append((extra, text)))
        view._draw_segment(0, _cell(slot=slot, dp=-1), n, False, CHANNEL | BIT)
        assert drawn == [(n - 1, label)]
    finally:
        view.deleteLater()


def _clash_lines(view):
    """The (x0, x1) of each clash X stroke in `view`'s scene."""
    from PySide6.QtWidgets import QGraphicsLineItem
    red = grid_view._CLASH_COLOR["bus"]
    return [sorted((it.line().x1(), it.line().x2())) for it in view._scene.items()
            if isinstance(it, QGraphicsLineItem) and it.pen().color() == red]


def test_a_clash_over_a_widened_cds_column_spans_the_columns_width():
    QApplication.instance() or QApplication([])
    view = GridView()
    cds = {"row": 0, "col": 0, "is_cds": True, "slot": 0, "dp": -1, "device": -1,
           "channel": -1, "sample": -1, "bit": 0, "is_source": True}
    view.set_bus_model([cds], {(0, 0): "bus"}, 4, 1, cds_symbol="CDS\nSPx EDEx")
    assert view._colw(0) > grid_view._CW                # the flags line widens it
    lines = _clash_lines(view)
    assert len(lines) == 2
    x = view._cx(0)
    assert all(pair == [x + 2, x + view._colw(0) - 2] for pair in lines)
