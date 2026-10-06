"""Timing view (EyeView) colour-key flavour toggle.

Clicking a clock/data edge colour-key entry hides/shows that flavour in the plot
and greys the key entry. This is view-level (Qt), so it lives here rather than in
the compute-only test_bus_timing.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_eye_view.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from swi3s_studio.session import Session
from swi3s_studio.ui.eye_view import EyeView, _cat_specs
from swi3s_studio.ui.theme import VizTheme


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


def test_a_cached_timing_pane_measures_each_region_on_its_own():
    """The app gives the pane a per-Link cache (so a Link switch does not re-scan the
    capture). The cache must be per REGION: one entry for the whole capture showed the
    first region's setup/hold whatever region was picked."""
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
