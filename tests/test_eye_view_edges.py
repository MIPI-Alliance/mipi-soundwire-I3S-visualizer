"""Timing (eye) view: histogram extent for a narrow side, and the driver-filter button label."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from swi3s_studio.session import Session
from swi3s_studio.ui.eye_view import EyeView

pytestmark = pytest.mark.gui


def _view():
    QApplication.instance() or QApplication([])
    ev = EyeView()
    s = Session.from_demo(64)
    ev.set_capture(s.capture)
    ev.set_analysis_context(s.segments, s.timing_regions(), s.timing_column_roles)
    ev._render()
    return ev


def _bar_extent(plot):
    xs = np.concatenate([np.asarray(i.getData()[0]) for i in plot.getPlotItem().listDataItems()])
    return float(xs.min()), float(xs.max())


def test_a_side_narrower_than_two_sample_periods_is_drawn_over_its_real_extent():
    """One bin spans [0, the side's largest margin under the filter]."""
    ev = _view()
    ev._rate = 1e6                                         # 1000 ns resolution: one bin a side
    ev._redraw()
    mask = ev._filter_mask(ev._timing)
    for plot, margins in ((ev._setup_plot, ev._timing.tr_setup_ns),
                          (ev._hold_plot, ev._timing.tr_hold_ns)):
        largest = float(margins[mask].max())
        lo, hi = _bar_extent(plot)
        assert lo == 0.0 and hi == pytest.approx(largest), (hi, largest)
        assert largest > 1.0


def test_renaming_a_port_relabels_the_filter_button_that_names_it():
    ev = _view()
    ports = [i for i in ev._filter_items if i.get("port") is not None]
    assert ports
    for it in ev._filter_items:
        it["action"].setChecked(it is ports[0])
    ev._update_filter_text()
    d, p = ports[0]["port"]
    assert ev._filter.text() == ports[0]["action"].text() + "  ▾"
    ev.set_port_names({(d, p): "Mic L"})
    assert "Mic L" in ev._filter.text()
