"""Timing view (EyeView) colour-key flavour toggle.

Clicking a clock/data edge colour-key entry hides/shows that flavour in the plot
and greys the key entry. This is view-level (Qt), so it lives here rather than in
the compute-only test_bus_timing.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 tests/test_eye_view.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from swi3s_studio.ui.eye_view import EyeView, _cat_specs
from swi3s_studio.ui.theme import VizTheme
from swi3s_studio.session import Session


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


if __name__ == "__main__":
    test_legend_has_all_four_flavours(); print("ok: colour key has all four edge flavours")
    test_toggle_hides_flavour_and_greys_key(); print("ok: click toggles a flavour + greys the key")
    test_all_flavours_hidden_renders_without_error(); print("ok: all-hidden renders cleanly")
    print("ALL EYE-VIEW TESTS PASSED")
