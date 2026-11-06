"""Tests for analysis.bus_timing — per-transition setup/hold from a synthetic capture
with known geometry. Validates the tr_* arrays the Timing pane actually consumes.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from swi3s_studio.analysis.bus_timing import measure_bus_timing
from swi3s_studio.ingest.capture import Capture

RATE = 250_000_000   # 4 ns/sample


def _synthetic(ui_samples=20, data_offset=4, n=4000):
    """Clock toggling every `ui_samples` samples; the data line transitions
    `data_offset` samples into each UI. So every UI: setup = (ui-offset), hold =
    offset, and the transition phase within the UI = offset/ui."""
    clk = np.arange(ui_samples, ui_samples * (n + 1), ui_samples, dtype=np.uint64)
    dat = clk + data_offset
    return Capture(clock_edges=clk, data_edges=dat.astype(np.uint64),
                   initial_clock=False, initial_data=False, sample_rate_hz=RATE)


def test_setup_hold_geometry():
    t = measure_bus_timing(_synthetic(ui_samples=20, data_offset=4))
    assert t is not None
    assert abs(t.ui_ns - 80.0) < 1.0, t.ui_ns
    # every transition: setup 64 ns ((20-4)*4), hold 16 ns (4*4)
    assert abs(float(np.median(t.tr_setup_ns)) - 64.0) < 2.0, np.median(t.tr_setup_ns)
    assert abs(float(np.median(t.tr_hold_ns)) - 16.0) < 2.0, np.median(t.tr_hold_ns)
    # both clock polarities carry the same geometry (setup keyed to the sampling edge,
    # hold to the previous edge)
    for want in (True, False):
        s = t.tr_setup_ns[t.tr_setup_clk_rising == want]
        h = t.tr_hold_ns[t.tr_hold_clk_rising == want]
        assert s.size > 100 and h.size > 100, (s.size, h.size)
        assert abs(float(np.median(s)) - 64.0) < 2.0, np.median(s)
        assert abs(float(np.median(h)) - 16.0) < 2.0, np.median(h)
    # each transition contributes exactly one setup and one hold — equal counts
    assert t.tr_setup_ns.size == t.tr_hold_ns.size == t.n_data_edges


def test_transition_phase():
    # phase within the UI = hold / (setup + hold) = offset/ui = 4/20 = 0.2
    t = measure_bus_timing(_synthetic(ui_samples=20, data_offset=4))
    phase = t.tr_hold_ns / (t.tr_setup_ns + t.tr_hold_ns)
    assert abs(float(np.median(phase)) - 0.2) < 0.05, float(np.median(phase))


def test_too_few_edges_returns_none():
    cap = Capture(clock_edges=np.arange(10, dtype=np.uint64),
                  data_edges=np.arange(5, dtype=np.uint64),
                  initial_clock=False, initial_data=False, sample_rate_hz=RATE)
    assert measure_bus_timing(cap) is None


def test_tight_setup():
    # data transitions 1 sample before the sample edge -> setup ~4 ns (near zero)
    t = measure_bus_timing(_synthetic(ui_samples=20, data_offset=19))
    assert float(np.percentile(t.tr_setup_ns, 5)) < 8.0, np.percentile(t.tr_setup_ns, 5)
    assert abs(float(np.median(t.tr_hold_ns)) - 76.0) < 2.0, np.median(t.tr_hold_ns)  # 19*4


def test_held_bits_no_multi_ui():
    # Data transitions only once every `stride` UIs (the line holds in between). Each
    # measured margin is bounded by its own UI (setup/hold <= UI), so a held run never
    # inflates a margin into a multi-UI gap; only the ~1/stride real transitions count.
    ui, off, stride, n = 20, 4, 5, 4000
    clk = np.arange(ui, ui * (n + 1), ui, dtype=np.uint64)
    dat = (clk[::stride] + off).astype(np.uint64)
    cap = Capture(clock_edges=clk, data_edges=dat,
                  initial_clock=False, initial_data=False, sample_rate_hz=RATE)
    t = measure_bus_timing(cap)
    assert t is not None
    assert abs(float(np.median(t.tr_setup_ns)) - 64.0) < 4.0, np.median(t.tr_setup_ns)
    assert abs(float(np.median(t.tr_hold_ns)) - 16.0) < 4.0, np.median(t.tr_hold_ns)
    assert float(t.tr_setup_ns.max()) < 1.5 * t.ui_ns, t.tr_setup_ns.max()   # no multi-UI
    assert t.n_data_edges < 0.6 * n, t.n_data_edges   # ~1/stride of the UIs transitioned


def test_region_bounds_exclude_out_of_range():
    # start_sample/end_sample restrict to a sub-range; transitions outside are dropped.
    t_all = measure_bus_timing(_synthetic(ui_samples=20, data_offset=4, n=4000))
    lo, hi = 20_000, 40_000                         # samples ~ UIs 1000..2000
    t = measure_bus_timing(_synthetic(ui_samples=20, data_offset=4, n=4000),
                           start_sample=lo, end_sample=hi)
    assert t is not None
    assert t.n_data_edges < t_all.n_data_edges
    assert t.tr_sample.min() >= lo and t.tr_sample.max() < hi
    assert abs(float(np.median(t.tr_setup_ns)) - 64.0) < 2.0   # geometry unchanged


def test_n_clock_edges_counts_the_region_not_the_capture():
    cap = _synthetic(ui_samples=20, data_offset=4, n=4000)
    whole = measure_bus_timing(cap)
    half = measure_bus_timing(cap, start_sample=20_000, end_sample=40_000)
    assert whole.n_clock_edges == 4000
    assert half.n_clock_edges == 1000        # clock edges in [20000, 40000), one per 20 samples


def _dlv_capture(rows=200, cols=16, ui=20, offset=3):
    """A DP wire toggling at every UI boundary (+offset), and the row-start samples."""
    row = cols * ui
    syncs = np.arange(rows + 1, dtype=np.int64) * row + 100
    dp = (syncs[:-1, None] + np.arange(cols)[None, :] * ui + offset).reshape(-1)
    cap = Capture(clock_edges=dp.astype(np.uint64), data_edges=dp.astype(np.uint64),
                  initial_clock=False, initial_data=False, sample_rate_hz=RATE)
    return cap, (syncs, float(ui)), [{"start_sample": int(syncs[0]), "start_ui": 0,
                                      "column_count": cols}]


def test_dlv_margins_match_the_mid_ui_grid_and_columns():
    cap, rc, segs = _dlv_capture()
    t = measure_bus_timing(cap, segments=segs, recovered_clock=rc)
    assert t is not None
    # A transition 3 samples after a UI boundary: the latch point 10 samples into the UI is
    # 7 samples later (setup 28 ns), and the previous latch point 13 samples earlier (hold
    # 52 ns).
    assert abs(float(np.median(t.tr_setup_ns)) - 28.0) < 0.5
    assert abs(float(np.median(t.tr_hold_ns)) - 52.0) < 0.5
    assert set(np.unique(t.tr_column).tolist()) <= set(range(16))
    assert abs(t.ui_ns - 80.0) < 0.5
    assert t.n_clock_edges == 200 * 16             # mid-UI points of the rows measured


def test_dlv_ui_estimate_uses_at_most_max_edges_rows():
    cap, (syncs, ui), segs = _dlv_capture(rows=50)
    row = 16 * 20
    lengths = np.where(np.arange(50) < 10, row, 2 * row)    # rows from 10 on are twice as long
    syncs = 100 + np.concatenate([[0], np.cumsum(lengths)])
    t_all = measure_bus_timing(cap, segments=segs, recovered_clock=(syncs, ui))
    t_few = measure_bus_timing(cap, segments=segs, recovered_clock=(syncs, ui), max_edges=5)
    assert t_few.ui_ns == 80.0 and t_all.ui_ns > 80.0


# ---------------------------------------------------------------------------
# Asymmetric geometry: every (clock polarity x data direction) category differs
# ---------------------------------------------------------------------------

def _eye_capture(groups=300):
    """A forwarded-clock capture with unequal phases (24 samples high, 16 low) whose
    transitions fall in four different categories, one offset each.

    Clock edge k is rising for even k (initial clock low). Group g places four
    transitions in the UIs ending at sample edges 10+8g + (0, 2, 3, 5): the first two
    are sampled by a rising edge, the last two by a falling edge, and the data
    direction alternates with the transition's index (even = rising). Offsets after the
    previous edge are 2, 5, 8, 11 samples."""
    n = 10 + 8 * groups + 8
    gaps = np.where(np.arange(n) % 2 == 0, 24, 16)           # the gap AFTER edge k
    clk = 20 + np.concatenate([[0], np.cumsum(gaps[:-1])]).astype(np.int64)
    end_i = (10 + 8 * np.arange(groups)[:, None] + np.array([0, 2, 3, 5])[None, :]).reshape(-1)
    off = np.tile(np.array([2, 5, 8, 11]), groups)
    dat = clk[end_i - 1] + off
    cap = Capture(clock_edges=clk.astype(np.uint64), data_edges=dat.astype(np.uint64),
                  initial_clock=False, initial_data=False, sample_rate_hz=RATE)
    return cap, clk, dat, end_i


def test_eye_arrays_pin_clock_polarity_data_direction_and_duty():
    cap, clk, dat, end_i = _eye_capture()
    t = measure_bus_timing(cap)
    assert t is not None and t.n_data_edges == dat.size
    j = np.arange(dat.size)
    assert t.tr_sample.tolist() == dat.tolist()
    assert np.allclose(t.tr_setup_ns, (clk[end_i] - dat) * 4.0)
    assert np.allclose(t.tr_hold_ns, (dat - clk[end_i - 1]) * 4.0)
    # The sampling edge is rising for an even edge index; the previous edge is the other.
    assert t.tr_setup_clk_rising.tolist() == (end_i % 2 == 0).tolist()
    assert t.tr_hold_clk_rising.tolist() == (end_i % 2 == 1).tolist()
    # The first transition drives the line high from its low initial level, then they alternate.
    assert t.tr_data_rising.tolist() == (j % 2 == 0).tolist()
    # The four categories are all present, each with its own setup: a UI ending in a
    # rising edge is a low phase (16 samples), one ending in a falling edge a high phase (24).
    combos = {(bool(c), bool(d)): float(np.median(t.tr_setup_ns[(t.tr_setup_clk_rising == c)
                                                                  & (t.tr_data_rising == d)]))
              for c in (True, False) for d in (True, False)}
    assert combos == {(True, True): 56.0, (True, False): 44.0,
                      (False, True): 64.0, (False, False): 52.0}
    # Clock phases: high after a rising edge, low after a falling one.
    assert t.clock_high_ns == 96.0 and t.clock_low_ns == 64.0


def test_eye_columns_count_from_the_segment_start():
    cap, clk, dat, end_i = _eye_capture()
    segs = [{"start_ui": 4, "column_count": 7}, {"start_ui": 1000, "column_count": 12}]
    t = measure_bus_timing(cap, segments=segs)
    want = np.where(end_i < 1000, (end_i - 4) % 7, (end_i - 1000) % 12)
    assert t.tr_column.tolist() == want.tolist()
    assert (measure_bus_timing(cap).tr_column == -1).all()


def test_the_edge_count_floors_are_exact():
    def cap(n_clk, n_dat):
        clk = np.arange(1, n_clk + 1, dtype=np.uint64) * 20
        dat = clk[10:10 + n_dat] + 4
        return Capture(clock_edges=clk, data_edges=dat, initial_clock=False,
                       initial_data=False, sample_rate_hz=RATE)
    assert measure_bus_timing(cap(99, 40)) is None
    assert measure_bus_timing(cap(100, 40)) is not None
    assert measure_bus_timing(cap(200, 15)) is None
    assert measure_bus_timing(cap(200, 16)) is not None


def test_dlv_columns_name_the_column_that_latches_each_transition():
    cap, rc, segs = _dlv_capture()
    t = measure_bus_timing(cap, segments=segs, recovered_clock=rc)
    # Row starts are at 100 + 320 r and columns are 20 samples wide; a transition just
    # after a column boundary is latched by that column.
    want = ((t.tr_sample - 100) // 20) % 16
    assert t.tr_column.tolist() == want.tolist()
    # A transition exactly at end_sample is outside the region.
    stop = int(t.tr_sample[40])
    cut = measure_bus_timing(cap, segments=segs, recovered_clock=rc, end_sample=stop)
    assert cut.tr_sample.max() < stop
