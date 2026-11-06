"""Decoded-Samples pane: the Binary column's grouping, the value filter's parsing, the
port selection across a rename, and the window across a capped append.

Pinned here:
- Binary groups four bits from the LSB, so each group is one digit of the Hex column;
- the value filter takes decimal with leading zeros and 0x / 0o / 0b prefixes; anything else
  applies no filter and marks the field;
- a Link rename keeps the port selection;
- an append that evicts rows from the top keeps the same rows in view.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_decoded_sample_view.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from swi3s_studio.ui.decoded_sample_view import DecodedSampleView, _binary, _hex

pytestmark = pytest.mark.gui

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _delete_the_views():
    """Delete, after each test, the parentless views it built, so a shown one does not stay
    a top-level widget of every later test. The class is patched by hand and restored here,
    not through `monkeypatch`, which the suite-wide window fixture shares and undoes itself."""
    from PySide6.QtCore import QCoreApplication, QEvent
    from shiboken6 import isValid
    made = []
    build = DecodedSampleView.__init__

    def recording_init(self, *args, **kwargs):
        build(self, *args, **kwargs)
        made.append(self)
    DecodedSampleView.__init__ = recording_init
    try:
        yield
    finally:
        DecodedSampleView.__init__ = build
        for v in made:
            if isValid(v):
                v.close()
                v.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)


def _rows(lo: int, hi: int) -> list:
    return [{"row": r, "start_sample": r, "device": 1, "dp": 1, "channel": 0,
             "sample_size": 16, "value": r, "signed": r} for r in range(lo, hi)]


@pytest.mark.parametrize("value, bits, binary, hex_", [
    (0x2AAAA, 18, "10 1010 1010 1010 1010", "0x2AAAA"),
    (0x2A, 6, "10 1010", "0x2A"),
    (0xA5, 8, "1010 0101", "0xA5"),
    (0x1, 1, "1", "0x1"),
    (0x3, 2, "11", "0x3"),
])
def test_binary_groups_align_with_the_hex_digits(value, bits, binary, hex_):
    assert _binary(value, bits) == binary
    assert _hex(value, bits) == hex_


def test_the_value_filter_parses_decimal_with_leading_zeros_and_prefixed_numbers():
    v = DecodedSampleView()
    v._op_cb.setCurrentText("≤")
    assert v.filters()[2] is None            # no value typed
    for text, want in (("10", 10), ("010", 10), ("0x10", 16), ("-5", -5), ("0", 0),
                       ("0o17", 15), ("0b101", 5), ("0X1f", 31), ("-0x10", -16)):
        v._val_edit.setText(text)
        pred = v.filters()[2]
        assert pred is not None and pred[1] == want, (text, pred)
        assert v._val_edit.styleSheet() == ""


def test_a_value_that_is_not_a_whole_number_applies_no_filter_and_marks_the_field():
    v = DecodedSampleView()
    v._op_cb.setCurrentText("≤")
    v._val_edit.setText("0.5")
    assert v.filters()[2] is None
    assert "border" in v._val_edit.styleSheet() and v._val_edit.toolTip()
    v._val_edit.setText("7")
    assert v.filters()[2] is not None and v._val_edit.styleSheet() == ""
    v._val_edit.setText("abc")
    assert v.filters()[2] is None and v._val_edit.styleSheet() != ""
    v._val_edit.setText("")
    assert v.filters()[2] is None and v._val_edit.styleSheet() == ""


@pytest.mark.parametrize("text", ["1_000", "0x_10", "\u0663", "\uff11", "0x", "0b2", "0o8",
                                  "1e3", "--1"])
def test_a_value_with_underscores_non_ascii_digits_or_a_bad_prefix_applies_no_filter(text):
    v = DecodedSampleView()
    v._op_cb.setCurrentText("≤")
    v._val_edit.setText(text)
    assert v.filters()[2] is None, text
    assert "border" in v._val_edit.styleSheet() and v._val_edit.toolTip()


def test_a_rename_keeps_the_port_selection():
    v = DecodedSampleView()
    v.set_lanes([(1, 1, 0), (1, 2, 0)])
    act = next(a for a in v._port_cb._menu.actions() if tuple(a.data() or ()) == (1, 2))
    act.setChecked(True)
    assert v._port_cb.selected() == [(1, 2)]
    v.set_stream_names({(1, 2): "Mic"})
    assert [tuple(p) for p in v._port_cb.selected()] == [(1, 2)]
    assert v._port_cb.text() == "1 selected"


def test_a_capped_append_keeps_the_same_rows_in_view():
    v = DecodedSampleView()
    v.set_sample_rate(1e6)
    v.set_lanes([(1, 1, 0)])
    cap = v._MAX_LOADED
    v.set_samples(_rows(0, cap))
    v.resize(900, 300)
    v.show()
    QApplication.processEvents()
    sb = v._table.verticalScrollBar()
    sb.setValue(sb.maximum())
    top = sb.value()
    first_visible = v._samples[top]["start_sample"]
    evicted = v.append_samples(_rows(cap, cap + 2000))
    QApplication.processEvents()
    assert evicted == 2000 and v._table.rowCount() == cap
    assert v._samples[sb.value()]["start_sample"] == first_visible
    assert sb.value() == top - evicted
