"""Open Capture dialog: figures round before they pick a unit, thresholds must be finite,
and a typed Link name is compared the way it is validated."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from test_open_capture_dialog import _dlg, _probe

from swi3s_studio.ui.open_capture_dialog import _dur, _edges, _gb

pytestmark = pytest.mark.gui


def test_a_figure_picks_its_unit_after_rounding():
    assert _edges(999_800, True) == "~1.0M" and _edges(999_400, True) == "~999k"
    assert _gb(999_700_000) == "1.0 GB" and _gb(999_000_000) == "999 MB"
    assert _dur(0.9999996) == "1.000 s" and _dur(0.0009999996) == "1.000 ms"
    assert _dur(0.5) == "500.000 ms" and _dur(2e-4) == "200.000 µs"


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "1e400"])
def test_a_threshold_that_is_not_a_finite_number_is_refused(bad):
    dlg = _dlg(_probe(n=3, kind="analog_csv", analog=True))
    ch, cl, _dh, _dl = dlg.rows()[0].thresh
    ch.setText(bad)
    cl.setText("0.4")
    assert not dlg._open_btn.isEnabled() and "number of volts" in dlg._problem.text()


def test_a_trailing_space_in_a_typed_name_is_not_a_clash():
    dlg = _dlg()
    dlg._add_row_from_free()
    first, second = dlg.rows()
    first.name.setText("Link 1 ")
    first.name.textEdited.emit("Link 1 ")
    assert second.name.text() == "Link 2" and not second.name_edited
    assert dlg._open_btn.isEnabled(), dlg._problem.text()
