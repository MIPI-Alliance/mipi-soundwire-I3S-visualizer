"""Timing view (EyeView) colour-key flavour toggle, worst-UI ranking and axis ticks.

Clicking a clock/data edge colour-key entry hides/shows that flavour in the plot
and greys the key entry. This is view-level (Qt), so it lives here rather than in
the compute-only test_bus_timing. The View Worst UI list ranks transitions by the
smaller of setup and hold, then by time, keeping the 500 smallest; the x ticks step by
1/2/5·10ⁿ from 0 to the end, and the count ticks are decades.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_eye_view.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from swi3s_studio.session import Session
from swi3s_studio.ui.eye_view import EyeView, _cat_specs
from swi3s_studio.ui.theme import VizTheme

pytestmark = pytest.mark.gui


def _view():
    QApplication.instance() or QApplication([])
    ev = EyeView()
    s = Session.from_demo(64)
    ev.set_capture(s.capture)
    ev.set_analysis_context(s.segments, s.timing_regions(), s.timing_column_roles)
    ev._render()
    return ev


def test_legend_has_all_four_flavours():
    ev = _view()
    keys = {(ck, dk) for ck, dk, _l, _c in _cat_specs()}
    assert set(ev._legend_labels) == keys and len(keys) == 4


def test_toggle_hides_flavour_and_greys_key():
    ev = _view()
    ck, dk, _label, _color = _cat_specs()[0]
    # Nothing hidden initially.
    assert ev._cat_hidden == set()
    # Click hides the flavour; the key entry greys to TEXT_DIM.
    ev._toggle_category(ck, dk)
    assert ev._cat_hidden == {(ck, dk)}
    html = ev._legend_labels[(ck, dk)].text().lower()
    assert VizTheme.TEXT_DIM.lstrip("#").lower() in html
    # A still-shown flavour keeps its full colour (not the dim colour).
    other = next(k for k in ev._legend_labels if k != (ck, dk))
    assert VizTheme.TEXT_DIM.lstrip("#").lower() not in ev._legend_labels[other].text().lower()
    # Click again shows it; render stays stable with 0 or all hidden.
    ev._toggle_category(ck, dk)
    assert ev._cat_hidden == set()


def test_all_flavours_hidden_renders_without_error():
    ev = _view()
    for ck, dk, _l, _c in _cat_specs():
        ev._toggle_category(ck, dk)
    assert len(ev._cat_hidden) == 4
    ev._render()                      # empty plot, no crash
    assert ev._worst == []            # nothing visible -> no worst-UI candidates


def test_the_region_picker_is_wide_enough_for_its_longest_name():
    """The picker sizes to its regions whenever they change, so a long name is not clipped
    in the closed picker."""
    from PySide6.QtWidgets import QStyle, QStyleOptionComboBox
    QApplication.instance() or QApplication([])
    ev = EyeView()
    ev.resize(1600, 500)
    ev.show()
    QApplication.processEvents()
    s = Session.from_demo(300, cold_start=True)
    s.set_region_name(2, "A rather long name for the playback region")
    ev.set_analysis_context(s.segments, s.timing_regions(), s.timing_column_roles)
    QApplication.processEvents()
    combo = ev._region
    longest = max(range(combo.count()), key=lambda i: combo.fontMetrics().horizontalAdvance(
        combo.itemText(i)))
    combo.setCurrentIndex(longest)
    opt = QStyleOptionComboBox()
    combo.initStyleOption(opt)
    field = combo.style().subControlRect(QStyle.ComplexControl.CC_ComboBox, opt,
                                         QStyle.SubControl.SC_ComboBoxEditField, combo)
    assert field.width() >= combo.fontMetrics().horizontalAdvance(combo.currentText())


def test_a_cached_timing_pane_measures_each_region_on_its_own():
    """The app gives the pane a per-Link cache (so a Link switch does not re-scan the
    capture). The cache is per REGION: each region shows its own setup/hold."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.session import Session
    from swi3s_studio.ui import eye_view
    from swi3s_studio.ui.eye_view import EyeView

    s = Session.from_demo(600, cold_start=True, phy=3)
    regions = s.timing_regions()
    assert len(regions) >= 2
    ev = EyeView()
    ev.set_analysis_context(s.segments, regions, s.timing_column_roles)
    ev.set_capture(s.capture, recovered_clock=s.recovered_clock(), cache={})
    seen = []
    for i in range(len(regions)):
        ev._region.setCurrentIndex(i)
        ev._render()
        seen.append(ev._timing)
    uncached = EyeView()
    uncached.set_analysis_context(s.segments, regions, s.timing_column_roles)
    uncached.set_capture(s.capture, recovered_clock=s.recovered_clock())
    for i, t in enumerate(seen):
        uncached._region.setCurrentIndex(i)
        uncached._render()
        assert t.n_data_edges == uncached._timing.n_data_edges, regions[i]["label"]
    assert len({t.n_data_edges for t in seen}) == len(seen)       # really different
    calls = []
    real = eye_view.measure_bus_timing
    eye_view.measure_bus_timing = lambda *a, **k: calls.append(1) or real(*a, **k)
    try:
        for i in range(len(regions)):
            ev._region.setCurrentIndex(i)
            ev._render()
    finally:
        eye_view.measure_bus_timing = real
    assert not calls                                               # every region cached


# ---- the worst-UI ranking and the axis ticks ----

def test_the_worst_uis_rank_by_the_smaller_side_then_by_time():
    import numpy as np
    sample = np.array([10, 40, 30, 20, 50])
    setup = np.array([5.0, 1.0, 3.0, 1.0, 9.0])
    hold = np.array([2.0, 7.0, 3.0, 8.0, 0.5])            # margins 2, 1, 3, 1, 0.5
    assert EyeView._compute_worst(sample, setup, hold) == [
        (50, 9.0, 0.5), (20, 1.0, 8.0), (40, 1.0, 7.0), (10, 5.0, 2.0), (30, 3.0, 3.0)]
    assert [w[0] for w in EyeView._compute_worst(sample, setup, hold, take=3)] == [50, 20, 40]
    assert EyeView._compute_worst(sample[:0], setup[:0], hold[:0]) == []


def test_the_worst_list_keeps_the_500_smallest_margins():
    import numpy as np
    n = 600
    margin = np.arange(n, 0, -1, dtype=float)              # the last sample is the worst
    got = EyeView._compute_worst(np.arange(n), margin, margin + 1.0)
    assert len(got) == 500
    assert [w[0] for w in got[:3]] == [599, 598, 597] and got[-1][0] == 100


@pytest.mark.parametrize("hi, labels", [
    (10.0, ["0", "5", "10"]), (8.0, ["0", "2", "4", "6", "8"]), (1.0, ["0", "0.5", "1"]),
    (0.3, ["0", "0.1", "0.2", "0.3"]), (7.0, ["0", "2", "4", "6"]), (0.0, ["0"]),
    (-1.0, ["0"])])
def test_the_x_ticks_step_by_1_2_or_5_from_0_up_to_the_end(hi, labels):
    assert [lab for _v, lab in EyeView._x_ticks(hi)] == labels


@pytest.mark.parametrize("count, labels", [
    (0, ["1", "10"]), (9, ["1", "10"]), (99, ["1", "10", "100"]),
    (999, ["1", "10", "100", "1k"]), (1000, ["1", "10", "100", "1k", "10k"]),
    (2_000_000, ["1", "10", "100", "1k", "10k", "100k", "1M", "10M"])])
def test_the_count_ticks_are_decades_up_to_the_one_above_the_count(count, labels):
    ticks = EyeView._decade_ticks(count)
    assert [lab for _k, lab in ticks] == labels
    assert [k for k, _lab in ticks] == [float(k) for k in range(len(labels))]


def _wheel(plot, notches):
    """Scroll `notches` wheel notches (up is positive) over the middle of `plot`."""
    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    vp = plot.viewport()
    c = QPointF(vp.width() / 2, vp.height() / 2)
    ev = QWheelEvent(c, QPointF(vp.mapToGlobal(c.toPoint())), QPoint(0, 0),
                     QPoint(0, 120 * notches), Qt.NoButton, Qt.NoModifier,
                     Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(vp, ev)
    QApplication.processEvents()


def test_the_wheel_zooms_one_side_with_0_at_the_join_and_reset_restores_both():
    ev = _view()
    ev.resize(1200, 500)
    ev.show()
    QApplication.processEvents()
    full_setup = ev._setup_plot.viewRange()[0]
    full_hold = ev._hold_plot.viewRange()[0]
    assert not ev._zoom_reset.isEnabled()
    _wheel(ev._hold_plot, 3)
    lo, hi = ev._hold_plot.viewRange()[0]
    assert lo == 0 and hi < full_hold[1] / 1.9
    assert ev._setup_plot.viewRange()[0] == full_setup          # the other side stays
    ticks = ev._hold_plot.getAxis("bottom")._tickLevels[0]
    assert ticks[0] == (0.0, "0") and ticks[-1][0] <= hi
    assert ev._zoom_reset.isEnabled()
    _wheel(ev._setup_plot, 2)
    assert ev._setup_plot.viewRange()[0][1] < full_setup[1]
    _wheel(ev._hold_plot, -50)                                  # out past the whole span
    assert ev._hold_plot.viewRange()[0] == full_hold and "hold" not in ev._x_span
    ev._zoom_reset.click()
    assert ev._setup_plot.viewRange()[0] == full_setup and not ev._zoom_reset.isEnabled()


@pytest.mark.parametrize("shown", [True, False])
def test_a_theme_switch_keeps_the_zoom_of_a_side(shown):
    ev = _view()
    ev.resize(1200, 500)
    if shown:
        ev.show()
    QApplication.processEvents()
    _wheel(ev._hold_plot, 3)
    zoomed = ev._hold_plot.viewRange()[0]
    assert ev._zoom_reset.isEnabled() and "hold" in ev._x_span
    ev.retheme()
    ev.show()
    QApplication.processEvents()
    assert ev._hold_plot.viewRange()[0] == zoomed and "hold" in ev._x_span
    assert ev._zoom_reset.isEnabled()


def test_a_theme_switch_before_a_capture_is_measured_measures_it_when_shown():
    QApplication.instance() or QApplication([])
    ev = EyeView()
    s = Session.from_demo(64)
    ev.set_capture(s.capture)
    ev.set_analysis_context(s.segments, s.timing_regions(), s.timing_column_roles)
    ev.retheme()
    assert ev._timing is None
    ev.show()
    assert ev._timing is not None


def test_a_zoomed_side_is_binned_anew_and_stops_at_4_sample_periods():
    ev = _view()
    ev._render()
    res = 1e9 / ev._rate
    ev._zoom_x("setup", 120 * 200)
    assert ev._x_span["setup"] == pytest.approx(max(4 * res, ev._x_full["setup"] / 1000))
    # A filter change keeps the zoom; a new measurement (region or capture) drops it.
    ev._redraw()
    assert "setup" in ev._x_span
    ev._render()
    assert ev._x_span == {}
