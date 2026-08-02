"""Performance-regression benchmarks: wall-clock CEILINGS on the hot paths.

These are cliff detectors, not micro-benchmarks — a perf regression of the kind the
3.0.6 review found (multi-second engine builds, per-row CSV loops) should FAIL CI here
instead of being re-discovered at the next review. Ceilings are deliberately generous
(several x the observed time) to tolerate shared-CI-runner jitter; they catch order-of-
magnitude cliffs, not small drifts. Marked `perf` so they run in their own CI job
(`pytest -m perf`) and are excluded from the normal suite (`pytest -m "not perf"`).

Run: PYTHONPATH=. python3 -m pytest -m perf tests/test_perf.py
"""
import os
import tempfile
import time

import pytest
import swi3score

from swi3s_studio import decode
from swi3s_studio.ingest import digital_csv

pytestmark = pytest.mark.perf

_RATE = 98_304_000


def _elapsed(fn) -> float:
    t0 = time.perf_counter()
    fn()
    return time.perf_counter() - t0


def test_decode_demo_capture_ceiling():
    """Decode a few thousand UIs of synthetic bus — guards the C++ decode + Python glue."""
    levels = swi3score.make_demo_levels(2000)
    dt = _elapsed(lambda: decode(swi3score.MemorySampleSource(levels, _RATE, 4)))
    assert dt < 8.0, f"decode took {dt:.2f}s (ceiling 8s) — perf regression?"


def test_engine_build_ceiling():
    """Build the visualizer bus model at a moderate row count with several data ports —
    guards the engine's per-tick placement loop (O(rows x cols x DPs)) + the popcount."""
    from swi3s_studio.swviz.core.engine import BusModelBuilder
    from swi3s_studio.swviz.models.interface import Interface
    from swi3s_studio.swviz.viz import VizConfig

    def build():
        iface, viz = Interface(), VizConfig()
        iface.NumColumns_REG = 15
        for i in range(4):                       # 4 DPs in NON-overlapping column windows
            dp = iface.data_ports[i].config      # (clash-free, so no warning-log spam skews
            dp.EnableCh_REG = 0b1                #  the timing) — still ticks every rowxcol.
            dp.HorizontalStart_REG = 1 + i * 3
            dp.HorizontalCount_REG = 2
            viz.data_ports[i].enabled = True
            iface.set_dp_device(i, i)
        BusModelBuilder(iface, 512, viz).build()

    dt = _elapsed(build)
    assert dt < 15.0, f"engine build took {dt:.2f}s (ceiling 15s) — perf regression?"


def test_digital_csv_import_ceiling():
    """Vectorized digital-CSV import over ~100k rows — guards channel_transition_counts +
    infer_sample_rate + load_capture (all rewritten from per-row Python loops in 3.0.6)."""
    n = 100_000
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "big.csv")
        with open(p, "w") as f:
            f.write("Time [s], Clock, Data\n")
            f.writelines(f"{s / _RATE:.12g},{s & 1},{(s // 3) & 1}\n" for s in range(n))

        def run():
            digital_csv.channel_transition_counts(p)
            digital_csv.infer_sample_rate(p)
            digital_csv.load_capture(p, clock_col=1, data_col=2)

        dt = _elapsed(run)
    assert dt < 12.0, f"digital-CSV import took {dt:.2f}s (ceiling 12s) — perf regression?"


# --- Whole-capture-scan cliff detectors -------------------------------------
# The class of regression that keeps surfacing only in user testing: a per-cursor-move
# / per-toggle op that scans the WHOLE capture (O(#UIs)) instead of O(active/#edges).
# It hides from the other perf tests because they use tiny captures — these use a large
# SYNTHETIC capture (edges only, no decode) so the O(#UIs) path is unmistakably slow
# while the correct O(#edges) path stays sub-second. See docs/PERFORMANCE.md.

_BIG_UIS = 30_000_000     # clock edges (≈ UIs); big enough that an O(#UIs) scan is seconds


def _big_capture():
    """A large synthetic Capture — 30M clock edges (one per UI) + SPARSE data edges — for
    the whole-capture-scan ceilings. No decode; just the two edge arrays the scans walk."""
    import numpy as np

    from swi3s_studio.ingest.capture import Capture
    ce = np.arange(_BIG_UIS, dtype=np.uint64) * 4 + 10
    de = np.arange(0, _BIG_UIS, 500, dtype=np.uint64) * 4 + 12    # 1 data edge / 500 UIs
    return Capture(clock_edges=ce, data_edges=de, initial_clock=False,
                   initial_data=True, sample_rate_hz=500_000_000)


def test_link_control_decode_ceiling():
    """Cold/Warm-start detection must read only a bounded PREFIX of the edges — a bring-up
    is a few dozen slow pulses at the very start. Scanning + sorting the whole capture
    (both orientations) made a 420M-edge capture take ~26s (fix 4988d1c). Over 30M edges
    the prefix scan is ~1ms; a regression to the full sort/scan is ~1s+."""
    from swi3s_studio.analysis.link_control import decode_link_control
    dt = _elapsed(lambda: decode_link_control(_big_capture()))
    assert dt < 0.5, f"decode_link_control took {dt:.2f}s (ceiling 0.5s) — scanning all edges again?"


def test_tx_persist_columns_ceiling():
    """TX-map persistence must scan the region's DATA EDGES (O(#edges)), never every UI
    with np.logical_or.at — that made a long audio region take ~90s (fix e3397c4). Drive
    the scan over a 30M-UI region with sparse data edges: the correct path is <0.1s, an
    O(#UIs) regression is several seconds. (A throwaway demo session's capture/segment is
    swapped for the big synthetic one — tx_persist_columns only reads those two.)"""
    from swi3s_studio.session import Session
    cap = _big_capture()
    sess = Session.from_demo(64)
    sess.capture = cap
    sess.segments = [{"start_ui": 1, "column_count": 8,
                      "start_sample": int(cap.clock_edges[2]),
                      "end_sample": int(cap.clock_edges[-1]), "row_base": 0}]
    sess._tx_persist_cache = {}
    dt = _elapsed(lambda: sess.tx_persist_columns(int(cap.clock_edges[100])))
    assert dt < 1.5, f"tx_persist_columns took {dt:.2f}s (ceiling 1.5s) — scanning every UI again?"
