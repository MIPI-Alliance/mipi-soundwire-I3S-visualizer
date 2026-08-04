"""Export Capture: capture sub-range slicing, Session range conversions, per-signal
subset export, and the CaptureExportDialog wiring.

Run: python3 -m pytest tests/test_capture_export.py -q
"""
import os
import tempfile

import numpy as np
import swi3score
from PySide6.QtWidgets import QApplication

from swi3s_studio.ingest import raw_export, transitions
from swi3s_studio.ingest.capture import Capture
from swi3s_studio.ingest.transitions import build_capture_from_levels
from swi3s_studio.session import Session

_app = QApplication.instance() or QApplication([])


def _demo_session():
    return Session.from_demo(200, cold_start=True, phy=2)


def test_subcapture_rebases_and_keeps_level():
    cap = Capture(clock_edges=np.array([10, 20, 30, 40], dtype=np.uint64),
                  data_edges=np.array([15, 25], dtype=np.uint64),
                  initial_clock=False, initial_data=False, sample_rate_hz=1_000_000)
    sub = cap.subcapture(16, 35)
    np.testing.assert_array_equal(sub.clock_edges, np.array([4, 14], dtype=np.uint64))   # 20,30 - 16
    np.testing.assert_array_equal(sub.data_edges, np.array([9], dtype=np.uint64))         # 25 - 16
    # at sample 16 both lines have toggled once (clock@10, data@15) -> both high
    assert sub.initial_clock is True
    assert sub.initial_data is True
    assert sub.sample_rate_hz == 1_000_000


def test_session_range_helpers_monotonic():
    s = _demo_session()
    assert s.last_sample() > 0
    assert s.total_bus_rows() > 1 and s.total_uis() > 1
    assert s.sample_at_bus_row(0) <= s.sample_at_bus_row(s.total_bus_rows() - 1)
    assert 0 <= s.sample_at_ui(0) < s.sample_at_ui(s.total_uis() - 1) <= s.last_sample()


def test_export_csv_with_time_range():
    s = _demo_session()
    last = s.last_sample()
    with tempfile.TemporaryDirectory() as d:
        full = os.path.join(d, "full.csv")
        part = os.path.join(d, "part.csv")
        s.export_csv(full)
        s.export_csv(part, sample_range=(last // 4, last // 2))     # middle quarter
        nf = sum(1 for _ in open(full)); npart = sum(1 for _ in open(part))
    assert 1 < npart < nf                                           # a real, smaller slice


def test_export_bin_and_csv_signal_subset():
    cap = build_capture_from_levels(swi3score.make_demo_levels(64))
    with tempfile.TemporaryDirectory() as d:
        # CSV, clock only -> header is Time + one column
        p = os.path.join(d, "clk.csv")
        raw_export.export_csv(cap, p, clock_name="CLK", include_data=False)
        assert open(p).readline().strip() == "Time [s],CLK"
        # .bin, data only -> exactly one file written
        written = raw_export.export_bin(cap, os.path.join(d, "x.bin"), include_clock=False)
        assert len(written) == 1 and written[0].endswith("-digital-1.bin")


def test_capture_export_dialog():
    from swi3s_studio.ui.capture_export_dialog import CaptureExportDialog
    s = _demo_session()
    with tempfile.TemporaryDirectory() as d:
        dlg = CaptureExportDialog(s, default_dir=d, clock_name="DP", data_name="DN")
        # default: .sal, both signals (forced+locked for .sal), whole capture
        assert dlg.fmt() == "sal"
        assert dlg.signal_names() == ("DP", "DN")
        assert dlg.include() == (True, True)
        assert dlg.sample_range() is None
        assert dlg.output_path().endswith(".sal")
        assert not dlg._clk_on.isEnabled()                          # .sal locks the pair

        # switch to CSV -> extension follows, signals become selectable
        dlg._format.setCurrentIndex(2)
        assert dlg.fmt() == "csv" and dlg.output_path().endswith(".csv")
        assert dlg._clk_on.isEnabled()

        # a bus-row range converts to a sample window via the session
        ridx = [dlg._range.itemData(i) for i in range(dlg._range.count())].index("rows")
        dlg._range.setCurrentIndex(ridx)
        dlg._start.setValue(2)
        dlg._end.setValue(10)
        rng = dlg.sample_range()
        assert rng == (s.sample_at_bus_row(2), s.sample_at_bus_row(10))
        assert rng[1] > rng[0]


def test_subcapture_folds_edge_at_window_start():
    """An edge landing exactly at the window start folds into the initial level (it
    would otherwise become a zero-gap edge at sample 0 that the .sal v3 codec rejects)."""
    cap = Capture(clock_edges=np.array([15, 25], dtype=np.uint64),
                  data_edges=np.array([40], dtype=np.uint64),
                  initial_clock=False, initial_data=False, sample_rate_hz=1_000_000)
    sub = cap.subcapture(15, 35)
    np.testing.assert_array_equal(sub.clock_edges, np.array([10], dtype=np.uint64))  # 25-15; 15 folded
    assert 0 not in sub.clock_edges.tolist()
    assert sub.initial_clock is True                                  # edge at 15 folded -> high


def test_export_sal_tolerates_leading_zero_edge():
    """A capture whose first edge is at sample 0 must still export as .sal (the v3
    codec needs deltas >= 1): the edge folds into the initial level."""
    from swi3s_studio.ingest import sal_export, saleae_sal
    cap = Capture(clock_edges=np.array([0, 100, 200], dtype=np.uint64),
                  data_edges=np.array([50, 150], dtype=np.uint64),
                  initial_clock=False, initial_data=False, sample_rate_hz=1_000_000)
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "z.sal")
        sal_export.export_sal(cap, p)                                 # must not raise
        c2 = saleae_sal.load_capture(p, 0, 1)
    assert bool(c2.initial_clock) is True                             # edge@0 folded -> flipped init
    np.testing.assert_array_equal(c2.clock_edges.astype(np.int64), np.array([100, 200]))


def test_export_sal_with_range_on_dlv():
    """.sal export of a sub-range whose bounds land on DLV row-sync (RSP) edges must
    not crash (regression: 'v3 delta must be >= 1') and must round-trip."""
    from swi3s_studio.ingest import saleae_sal
    cap = transitions.demo_capture(600, phy=3)
    s = Session(cap, dlv=True, forced_column_count=16)
    rng = (s.sample_at_bus_row(40), s.sample_at_bus_row(200))         # bounds are RSP edges
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "range.sal")
        s.export_sal(p, sample_range=rng)                             # regression: used to raise
        c2 = saleae_sal.load_capture(p, 0, 1)
    assert c2.clock_edges.size > 0


def test_partial_dlv_grid_shows_frame():
    """A partial DLV capture (forced, single operational segment, no config commits)
    must draw the full frame — Sync1 at Column 0, the CDS at Column 2, and Sync0 at the
    last column — not the collapsed 2-column S1/S0 default (no CDS, Sync0 at col 1)."""
    cap = transitions.demo_capture(600, phy=3)
    s = Session(cap, dlv=True, forced_column_count=16)
    assert s._dlv_forced and s.column_count == 16
    cells = s.grid_cells_at(s.sample_at_bus_row(50), 4)
    assert sorted(set(c["col"] for c in cells if c.get("is_cds"))) == [2]   # CDS column shown
    r0 = min(c["row"] for c in cells)
    syncs = {c["slot"]: c["col"] for c in cells if c["row"] == r0 and c.get("slot") in (7, 8)}
    assert syncs.get(8) == 0                                          # slot 8 = Sync1 at Column 0
    assert syncs.get(7) == s.column_count - 1                         # slot 7 = Sync0 at last column
