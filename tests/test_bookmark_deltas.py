"""Bookmark deltas: ΔUI and ΔRows only where they mean something.

Within one Link they are COUNTS of the UIs and rows between the two bookmarks. Across two
Links, row and UI numbers share no origin, so they are elapsed time at a rate the two Links
SHARE, and each is shown only when they do: one UI (row) rate on both Links, across the
whole interval between the bookmarks.

What these pin is that "share" is judged on the timing IN FORCE there. It used to be one
number per capture, the rate at its END, so a pair in a region running another geometry
borrowed the final rate: the PHY2 demo's 8-column region rows at 3.072 MRows/s, but its
capture ends at 16 columns and 1.536 MRows/s, which a flow-control Link in 16 columns also
runs, so ΔRows was shown, at the wrong rate.

The fixture is the two-Link demo pair at offset 0, one timeline at 500 MHz. Regions
(samples): PHY2 demo, 8 columns [1,279,847, 2,321,513) and 16 columns from 2,321,513; the
flow-control demo, 16 columns from 1,417,786. Both run a 24.576 MHz UI throughout.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication


def _region(sess, cols):
    """(start, end, ui_hz, row_hz) of `sess`'s region running `cols` columns."""
    starts = {int(g["start_sample"]): int(g["column_count"]) for g in sess.segments}
    for start, end, ui, row in sess.rate_regions():
        if starts.get(start) == cols:
            return start, end, ui, row
    raise AssertionError(f"no {cols}-column region")


def _inside(region, frac):
    start, end = region[:2]
    return int(start + (end - start) * frac)


@pytest.fixture
def pair():
    from swi3s_studio.session import Session
    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo()                                      # PHY2, cold start
    fc = Session.from_demo(300, cold_start=True, phy=2, variant="flow_control",
                           register_map=win._rmap)
    win.load_session(fc, add_link=True)
    return win, win.links[0].session, fc


def _delta(win, s1, link1, s2, link2):
    win._bookmarks.clear()
    win._bookmarks.add(s1, link=link1)
    win._bookmarks.add(s2, link=link2)
    dui, drows, dt = win._bookmark_measure_rows()[-1][1:4]
    return dui, drows, dt


def test_the_regions_measure_the_rates_each_geometry_runs(pair):
    """The premise, and the fix's foundation: per-region rates, not one per capture."""
    _win, phy2, fc = pair
    assert _region(phy2, 8)[3] == pytest.approx(3_072_000, rel=1e-4)
    assert _region(phy2, 16)[3] == pytest.approx(1_536_000, rel=1e-4)
    assert _region(fc, 16)[3] == pytest.approx(1_536_000, rel=1e-4)
    for sess, cols in ((phy2, 8), (phy2, 16), (fc, 16)):
        assert _region(sess, cols)[2] == pytest.approx(24_576_000, rel=1e-4)
    assert phy2.row_rate_khz == pytest.approx(1536, rel=1e-4)   # the capture-wide number


def test_rows_are_withheld_where_the_row_rates_differ_and_uis_still_shown(pair):
    """Link 1 in 8 columns (3.072 MRows/s), Link 2 in 16 (1.536): no common row timing,
    so no ΔRows. Both run a 24.576 MHz UI, so ΔUI is shown. The capture-wide rates (1536
    and 1536) said otherwise."""
    win, phy2, fc = pair
    r8, r16 = _region(phy2, 8), _region(fc, 16)
    s1, s2 = 1_600_000, 2_000_000
    assert r8[0] < s1 < s2 < r8[1] and r16[0] < s1 < s2 < r16[1]
    dui, drows, dt = _delta(win, s1, 0, s2, 1)
    assert drows == "—"
    dt_s = (s2 - s1) / phy2.sample_rate_hz
    assert float(dui.replace(",", "")) == pytest.approx(dt_s * 24_576_000, rel=1e-3)


def test_both_are_shown_where_both_links_share_both_rates(pair):
    win, phy2, fc = pair
    s1, s2 = 2_600_000, 3_400_000                        # 16 columns on both Links
    for sess in (phy2, fc):
        assert _region(sess, 16)[0] < s1 < s2 < _region(sess, 16)[1]
    dui, drows, _dt = _delta(win, s1, 0, s2, 1)
    dt_s = (s2 - s1) / phy2.sample_rate_hz
    assert float(drows.replace(",", "")) == pytest.approx(dt_s * 1_536_000, rel=1e-3)
    assert float(dui.replace(",", "")) == pytest.approx(dt_s * 24_576_000, rel=1e-3)


def test_an_interval_across_a_geometry_change_has_no_common_row_rate(pair):
    """Link 1 changes from 8 to 16 columns inside the interval: its row rate is not one
    number there, so ΔRows is withheld. The UI rate holds, so ΔUI is shown."""
    win, phy2, fc = pair
    s1, s2 = 2_000_000, 2_600_000                        # Link 1: 8 -> 16 columns
    assert _region(phy2, 8)[0] < s1 < _region(phy2, 16)[0] < s2
    assert _region(fc, 16)[0] < s1                       # Link 2: 16 throughout
    dui, drows, _dt = _delta(win, s1, 0, s2, 1)
    assert drows == "—" and dui != "—"


def test_a_link_not_recording_throughout_the_interval_shares_nothing(pair):
    """Link 1's capture ends before Link 2's bookmark: nothing says what Link 1's timing
    was there, so neither delta is claimed. The time is still exact."""
    win, phy2, fc = pair
    s1 = _inside(_region(phy2, 16), 0.5)
    s2 = phy2.last_sample() + 100_000
    assert s2 < fc.last_sample()
    dui, drows, dt = _delta(win, s1, 0, s2, 1)
    assert dui == "—" and drows == "—" and dt != "—"


def test_a_bring_up_has_no_rows_to_share(pair):
    win, phy2, fc = pair
    dui, drows, _dt = _delta(win, 10_000, 0, 30_000, 1)  # both inside the cold starts
    assert dui == "—" and drows == "—"


def test_one_links_counts_are_exact_across_a_ui_rate_change():
    """Within one Link ΔUI is a count. On DLV the UI rate rises fourfold at the Safe-Lock-4
    to 16-column commit, so the old sample gap over the capture-wide (final) UI rate counted
    the Safe-Lock stretch four times over."""
    from swi3s_studio.session import Session
    from swi3s_studio.ui.main_window import MainWindow
    QApplication.instance() or QApplication([])
    win = MainWindow()
    phy3 = Session.from_demo(300, cold_start=True, phy=3, register_map=win._rmap)
    win.load_session(phy3)
    sl, op = _region(phy3, 4), _region(phy3, 16)
    s1, s2 = _inside(sl, 0.2), _inside(op, 0.5)
    dui, drows, _dt = _delta(win, s1, 0, s2, 0)
    count = phy3.ui_index(s2) - phy3.ui_index(s1)
    assert dui == f"{count:,}"
    old = (s2 - s1) * phy3.ui_rate_hz / phy3.sample_rate_hz
    assert abs(old - count) / count > 0.1               # what it used to say: far off
    assert drows == f"{phy3.bus_row_for_sample(s2) - phy3.bus_row_for_sample(s1):,}"
