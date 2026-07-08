"""Tests for analysis.bus_timing — per-transition setup/hold from a synthetic capture
with known geometry. Validates the tr_* arrays the Timing pane actually consumes.
"""
import os, sys
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from swi3s_studio.ingest.capture import Capture
from swi3s_studio.analysis.bus_timing import measure_bus_timing

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


if __name__ == "__main__":
    test_setup_hold_geometry(); print("ok: per-transition setup/hold geometry (64/16 ns)")
    test_transition_phase(); print("ok: transition phase within UI (~0.2)")
    test_too_few_edges_returns_none(); print("ok: too few edges -> None")
    test_tight_setup(); print("ok: tight setup flagged (p5 < 8 ns)")
    test_held_bits_no_multi_ui(); print("ok: held bits -> no multi-UI margin pileup")
    test_region_bounds_exclude_out_of_range(); print("ok: start/end_sample bound the region")
    print("BUS TIMING TESTS PASSED")
