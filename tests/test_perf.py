"""Performance-regression benchmarks: wall-clock CEILINGS on the hot paths.

These are cliff detectors, not micro-benchmarks — an order-of-magnitude regression
(multi-second engine builds, per-row CSV loops) FAILS here. Ceilings are deliberately
generous (several x the observed time) to tolerate shared-CI-runner jitter; they catch
order-of-magnitude cliffs, not small drifts. Marked `perf` so they run in their own CI job
(`pytest -m perf`) and are excluded from the normal suite (`pytest -m "not perf"`).

Run: PYTHONPATH=. python3 -m pytest -m perf tests/test_perf.py
(A bare `pytest` skips these: pyproject.toml's addopts default is -m "not perf".)
"""
import gc
import os
import pathlib
import sys
import tempfile
import time

import pytest
import swi3score
from conftest import record_perf

from swi3s_studio import decode
from swi3s_studio.ingest import digital_csv

pytestmark = pytest.mark.perf


# GitHub's shared runners (GITHUB_ACTIONS=true) run the two whole-window cascades below
# slower than a development machine, so they get their own ceilings; the medians are in the
# `perf margins` section.
_SHARED_RUNNER = os.environ.get("GITHUB_ACTIONS") == "true"


@pytest.fixture(autouse=True)
def _no_gc_pause():
    """Collect first, then keep the cyclic GC out of the timed region. A gen-2 pass over
    whatever an earlier test left behind lands on one timed call at random and can double
    it; that cost is the host process, not the code under test."""
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        gc.enable()

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
    guards the engine's per-tick placement loop (O(rows x cols x DPs)) + the popcount.

    Speed is NOT a goal of the Python model. swi3score, the C++ core, is the fast
    implementation; swviz/models/dataport.py is a functional model of a hardware
    implementation, published in the MIPI specification, and it optimises for a spec
    reader's comprehension instead. The ceiling below is a runaway guard (an accidental
    O(n^2), a re-decode per tick), not a defence of micro-optimisation.
    """
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
    """Vectorized digital-CSV import over ~100k rows — guards scan +
    infer_sample_rate + load_capture (all vectorized, no per-row Python loops)."""
    n = 100_000
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "big.csv")
        with open(p, "w") as f:
            f.write("Time [s], Clock, Data\n")
            f.writelines(f"{s / _RATE:.12g},{s & 1},{(s // 3) & 1}\n" for s in range(n))

        def run():
            digital_csv.scan(p)
            digital_csv.infer_sample_rate(p)
            digital_csv.load_capture(p, clock_col=1, data_col=2)

        dt = _elapsed(run)
    assert dt < 12.0, f"digital-CSV import took {dt:.2f}s (ceiling 12s) — perf regression?"


# --- Whole-capture-scan cliff detectors -------------------------------------
# A per-cursor-move / per-toggle op must not scan the WHOLE capture (O(#UIs)) instead of
# O(active/#edges).
# It hides from the other perf tests because they use tiny captures — these use a large
# SYNTHETIC capture (edges only, no decode) so the O(#UIs) path is unmistakably slow
# while the correct O(#edges) path stays sub-second.

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
    is a few dozen slow pulses at the very start. The cost is independent of the capture's
    length."""
    from swi3s_studio.analysis.link_control import decode_link_control
    dt = _elapsed(lambda: decode_link_control(_big_capture()))
    assert dt < 0.5, (f"decode_link_control took {dt:.2f}s (ceiling 0.5s) — "
                      "decode_link_control must not scan every edge")


def test_tx_persist_columns_ceiling():
    """TX-map persistence must scan the region's DATA EDGES (O(#edges)), never every UI
    with np.logical_or.at. Drive the scan over a 30M-UI region with sparse data edges; the
    cost follows the data edges, not the UI count. (A throwaway demo
    session's capture/segment is swapped for the big synthetic one — tx_persist_columns
    only reads those two.)"""
    from swi3s_studio.session import Session
    cap = _big_capture()
    sess = Session.from_demo(64)
    sess.capture = cap
    sess.segments = [{"start_ui": 1, "column_count": 8,
                      "start_sample": int(cap.clock_edges[2]),
                      "end_sample": int(cap.clock_edges[-1]), "row_base": 0}]
    sess._tx_persist_cache = {}
    dt = _elapsed(lambda: sess.tx_persist_columns(int(cap.clock_edges[100])))
    assert dt < 1.5, (f"tx_persist_columns took {dt:.2f}s (ceiling 1.5s) — "
                      "tx_persist_columns must not scan every UI")


def test_command_filter_first_keystroke_ceiling():
    """The free-text filter's FIRST sweep after a model reset, which is the expensive one.

    QSortFilterProxyModel re-tests every source row on the first invalidateFilter, and the
    row-text cache is cold after set_commands/set_peripheral_maps — so the first character
    typed into Filter ▸ Expression renders the whole table on the GUI thread, after every
    load, re-decode and Clear All Filters. The row string is built via _display_text (no
    QModelIndex, no role dispatch), not through self.data(self.index(row, col)) for all
    twelve columns.

    A CEILING, NOT A BENCHMARK: generous enough for a loaded runner, tight enough to catch
    a row string built through the index and role path at 50k rows.
    """
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.model.registers import RegisterMap
    from swi3s_studio.session import Session
    from swi3s_studio.ui.command_table import CommandFilterProxy, CommandTableModel

    QApplication.instance() or QApplication([])
    cmds = list(Session.from_demo(2000).commands)
    assert cmds, "the demo produced no commands to filter"
    rows = 50_000
    model = CommandTableModel((cmds * (rows // len(cmds) + 1))[:rows],
                             RegisterMap.load(), 1_000_000)
    proxy = CommandFilterProxy()
    proxy.setSourceModel(model)

    def first_sweep():
        proxy.set_text("wr")
        proxy.rowCount()          # forces the filter to run over every source row

    dt = _elapsed(first_sweep)
    assert proxy.rowCount() > 0, "the filter matched nothing — the measurement is hollow"
    assert dt < 3.0, (
        f"the first filter keystroke took {dt:.2f}s over {rows:,} commands (ceiling 3s). "
        "The row cache must be built through _display_text, not data()/index() per cell, "
        "which runs on the GUI thread.")


def test_guard_suppressing_build_stays_linear_in_rows():
    """A build whose data writes suppress their own guards, once per row.

    `remove_bits_matching` defers its removals to one `compact()`, so the build stays linear
    in the row count; rebuilding the whole `bits` list per call is QUADRATIC.

    Asserts the SHAPE, not just a ceiling: a wall-clock limit alone would pass on a fast
    machine even if the quadratic term came back, so this checks that 4x the rows costs well
    under 8x the time. The ceiling is there too, for the case where everything is slow.
    """
    import logging

    from swi3s_studio.swviz.core.engine import BusModelBuilder
    from swi3s_studio.swviz.models.interface import Interface
    from swi3s_studio.swviz.viz import VizConfig

    logging.disable(logging.CRITICAL)
    try:
        def build(rows):
            iface, viz = Interface(), VizConfig()
            iface.NumColumns_REG = 30
            for slot in (0, 1):
                dp = iface.data_ports[slot].config
                dp.EnableCh_REG = 0b11
                dp.SampleSize_REG = 7
                dp.HorizontalStart_REG = 1 + slot * 12
                dp.HorizontalCount_REG = 10
                dp.Interval_REG = 0
                dp.GuardEnable_REG = True
                dp.TailWidth_REG = 1
                viz.data_ports[slot].enabled = True
                iface.set_dp_device(slot, 3)
            return BusModelBuilder(iface, rows, viz).build()

        small = _elapsed(lambda: build(500))
        large = _elapsed(lambda: build(2000))          # 4x the rows
        assert large < 8.0, f"a 2000-row guarded build took {large:.2f}s (ceiling 8s)"
        # Guard the growth rate, with room for timer noise on a tiny `small`.
        ratio = large / max(small, 1e-3)
        assert ratio < 8.0, (
            f"4x the rows cost {ratio:.1f}x the time ({small:.2f}s -> {large:.2f}s): the "
            "cost must stay linear: bus_model.bits must not be rebuilt per removal")
    finally:
        logging.disable(logging.NOTSET)


def test_low_entropy_locate_ceiling():
    """The tight wall-clock ceiling for the low-entropy sub-capture search.

    A constant (never-toggling) sub-pattern makes almost every offset in the main capture an
    exact match, which without a cap on collected hits is O(N * hits). tests/test_subcapture.py
    keeps a LOOSE bound in the default suite — a hang is worth catching on every config — and
    the ceiling that would notice a slowdown lives here, where runner jitter cannot redden an
    ordinary run. The ceiling is a cliff detector, set per platform class.
    """
    import numpy as np

    from swi3s_studio.analysis.subcapture import locate_subcapture
    from swi3s_studio.ingest.capture import Capture

    n = 8_000_000
    clk = (np.arange(1, n + 1, dtype=np.int64) * 2).astype(np.uint64)
    main = Capture(clock_edges=clk, data_edges=np.zeros(0, dtype=np.uint64),
                   initial_clock=False, initial_data=False, sample_rate_hz=1_000_000)
    sub_clk = (np.arange(1, 601, dtype=np.int64) * 2).astype(np.uint64)
    sub = Capture(clock_edges=sub_clk, data_edges=np.zeros(0, dtype=np.uint64),
                  initial_clock=False, initial_data=False, sample_rate_hz=1_000_000)
    # A data line that toggles a sample after every clock edge matches nothing in `main` (whose
    # data line is constant) and is not the complement of its clock (a PHY3 pair is refused).
    sub_no_match = Capture(clock_edges=sub_clk, data_edges=sub_clk + np.uint64(1),
                           initial_clock=False, initial_data=False,
                           sample_rate_hz=1_000_000)

    found = {}

    def both():
        found["exact"] = locate_subcapture(main, sub)              # the exact-match path (capped)
        found["none"] = locate_subcapture(main, sub_no_match)      # the seed path (no matches)

    dt = _elapsed(both)
    assert found["exact"] and all(m["score"] == 1.0 for m in found["exact"]), (
        "the constant sub-pattern matched nothing: the exact-match path was not timed")
    assert found["none"] == [], "the toggling sub-pattern matched: the seed path was not timed"
    assert dt < 1.0, f"low-entropy locate took {dt:.2f}s (ceiling 1s) — perf regression?"


def test_decoded_samples_rebuild_ceiling():
    """A cursor move that leaves the Samples window rebuilds the pane — bound that rebuild.

    The Decoded Samples pane is a QTableWidget refilled WHOLESALE, not a virtual model, so
    re-centring costs 2 * _SAMPLE_HALF_WINDOW * len(_COLS) fresh QTableWidgetItems, on the
    GUI thread, on every cursor move that leaves the loaded window.

    Two things can redden this: raising _SAMPLE_HALF_WINDOW, or making _fill_row more
    expensive per cell. The ceiling is loose because offscreen Qt elides the per-item layout
    that dominates on a real display — it is here to catch the order of magnitude, and 8000
    rows would need ~9x the budget.
    """
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.decoded_sample_view import DecodedSampleView
    from swi3s_studio.ui.main_window import _SAMPLE_HALF_WINDOW

    QApplication.instance() or QApplication([])

    assert _SAMPLE_HALF_WINDOW <= 1000, (
        f"_SAMPLE_HALF_WINDOW is {_SAMPLE_HALF_WINDOW}: the pane rebuild is O(window) and a "
        "bigger window only buys a CHANCE of staying inside it, so this trades the common "
        "case away for the rare one. See the constant's comment in main_window.")

    view = DecodedSampleView()
    view.set_sample_rate(_RATE)
    rows = [{"start_sample": i * 64, "value": 0xABCD, "sample_size": 16, "device": 1,
             "dp": 2, "channel": 0, "row": i, "signed": -1} for i in range(2 * _SAMPLE_HALF_WINDOW)]
    view.set_samples(rows)                      # warm the font metrics / column sizing
    dt = _elapsed(lambda: view.set_samples(rows))
    assert view.shown_span() is not None, "the pane loaded nothing — the measurement is hollow"
    assert dt < 1.0, (
        f"the Samples pane rebuild took {dt:.2f}s for {len(rows):,} rows (ceiling 1s) — "
        "a wholesale QTableWidget refill on the GUI thread, on every cursor move that "
        "leaves the loaded window.")


def test_tx_persist_inline_threshold_stays_inline():
    """_TX_PERSIST_SYNC_MAX_UIS must bound the scan to an INLINE budget, not to what the
    scan can eventually manage.

    TX-map persistence scans a whole config region. main_window computes it on the GUI
    thread when the region is at or below this many UIs and on a worker thread above, so
    the constant IS the freeze a cursor move can cause.

    This drives the scan over a region of exactly the threshold size and holds it to a
    ceiling a user would not notice, so raising the constant reddens the gate. The ceiling
    is generous for a loaded runner; test_tx_persist_columns_ceiling separately guards the
    scan's own O(#edges) behaviour at a much larger size.
    """
    import numpy as np

    from swi3s_studio.ingest.capture import Capture
    from swi3s_studio.session import Session
    from swi3s_studio.ui.main_window import _TX_PERSIST_SYNC_MAX_UIS

    n = int(_TX_PERSIST_SYNC_MAX_UIS)
    ce = (np.arange(1, n + 1, dtype=np.int64) * 4).astype(np.uint64)
    de = (np.arange(0, n, 500, dtype=np.int64) * 4 + 2).astype(np.uint64)
    cap = Capture(clock_edges=ce, data_edges=de, initial_clock=False,
                  initial_data=False, sample_rate_hz=_RATE)
    sess = Session.from_demo(64)
    sess.capture = cap
    sess.segments = [{"start_ui": 1, "column_count": 8,
                      "start_sample": int(ce[2]), "end_sample": int(ce[-1]),
                      "row_base": 0}]
    sess._tx_persist_cache = {}
    dt = _elapsed(lambda: sess.tx_persist_columns(int(ce[100])))
    assert dt < 0.35, (
        f"a region of {n:,} UIs -- the largest computed INLINE on the GUI thread -- took "
        f"{dt:.2f}s (ceiling 0.35s). Either the scan got slower or "
        f"_TX_PERSIST_SYNC_MAX_UIS was raised; every millisecond here is a cursor-move "
        f"freeze. Send bigger regions to _start_tx_persist_worker instead.")


def test_cursor_move_budget_with_every_dock_visible():
    """The WHOLE cursor cascade, with every dock open.

    Every other ceiling here times one function; this drives the cascade across every dock,
    all ten of them visible: raw view, register replay, command selection, CDS symbols,
    Decoded Samples, and the bus grid among them.

    THE FIXTURE SIZE IS THE POINT: on a small demo a capture-PROPORTIONAL per-move cost
    barely registers. from_demo(20000) holds over 5 M UIs and more than one config region,
    the scale of a multi-second real capture; the test asserts both.

    The ceilings are cliff detectors with headroom for a slower runner, not for drift; the
    medians are in the `perf margins` section.

    A KNOWN constant being raised is caught deterministically elsewhere: by
    _SAMPLE_HALF_WINDOW <= 1000 in test_decoded_samples_rebuild_ceiling and by the counts in
    tests/test_cursor_cost.py. This wall clock catches the cost nobody has bounded yet: a
    new scan on the cascade, a pane that stopped checking its dock, an O(capture) call added
    to a paint path.
    """
    import time

    from PySide6.QtWidgets import QApplication, QDockWidget

    from swi3s_studio.session import Session
    from swi3s_studio.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    w.resize(1800, 1000)
    w.show()
    w.load_session(Session.from_demo(20000, cold_start=True, register_map=w._rmap))
    docks = w.findChildren(QDockWidget)
    for d in docks:
        d.setVisible(True)
    app.processEvents()
    shown = sum(1 for d in docks if d.isVisible())
    assert shown == len(docks) >= 10, (
        f"{shown}/{len(docks)} docks visible — the cascade is not loaded on every dock")
    s = w._session
    assert len(s.capture.clock_edges) > 5_000_000, (
        f"fixture is only {len(s.capture.clock_edges):,} UIs — too small for a "
        "capture-proportional per-move cost to show, which is the whole reason it is this big")
    assert len(s.segments) >= 2, (
        f"fixture has {len(s.segments)} config region(s); the per-region cold paths "
        "(tx_persist_columns) need more than one to be exercised at all")

    ce = s.capture.clock_edges
    targets = [int(ce[int(len(ce) * f)]) for f in (0.1, 0.35, 0.6, 0.85, 0.2, 0.7, 0.45, 0.9)]
    w._on_cursor(targets[0])                      # warm caches / first-show costs
    app.processEvents()

    times = []
    for smp in targets:
        t0 = time.perf_counter()
        w._on_cursor(smp)
        app.processEvents()
        times.append(time.perf_counter() - t0)
    times.sort()
    median, worst = times[len(times) // 2], times[-1]
    ceiling, worst_ceiling = (0.24, 0.5) if _SHARED_RUNNER else (0.12, 0.25)
    record_perf("cursor move", median, ceiling, worst, worst_ceiling)
    assert median < ceiling, (
        f"median cursor move {median * 1000:.0f} ms across {shown} visible docks over "
        f"{len(ce):,} UIs (ceiling {ceiling * 1000:.0f} ms; worst "
        f"{worst * 1000:.0f} ms). "
        "Something on the cascade is doing work proportional to the capture on the GUI "
        "thread — see tests/test_cursor_cost.py for which counts to check first.")
    assert worst < worst_ceiling, (
        f"worst cursor move {worst * 1000:.0f} ms (ceiling {worst_ceiling * 1000:.0f} ms), "
        f"median {median * 1000:.0f} ms. A single move stalling while the rest are fast is a COLD "
        "path — a first-visit scan must run on a worker (_TX_PERSIST_SYNC_MAX_UIS), not inline.")



def test_link_switch_budget_with_every_dock_visible():
    """Switching Links with every dock open, on two Links at the cursor test's scale.

    A switch re-binds every per-Link pane to the other Link's cached state. The caches that
    make it cheap (the Timing pane's measurement, the command table's column fit, the audio
    store's gap-split envelope windows) are pinned DETERMINISTICALLY by counts in
    tests/test_links_gui.py, because a ceiling loose enough for a shared runner cannot see
    them. This is the cliff detector for what those counts do not cover, a new O(capture)
    step on the bind. The ceilings are cliff detectors; the medians are in the `perf margins`
    section.

    WINDOWS GETS ITS OWN CEILINGS, 1.5 s / 2.4 s. A shared GitHub runner (_SHARED_RUNNER)
    gets the same ceilings.
    """
    import sys
    import time

    from PySide6.QtWidgets import QApplication, QDockWidget

    from swi3s_studio.session import Session
    from swi3s_studio.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    w = MainWindow()
    w.resize(1800, 1000)
    w.show()
    w.load_session(Session.from_demo(20000, cold_start=True, register_map=w._rmap))
    w.load_session(Session.from_demo(20000, cold_start=True, variant="flow_control",
                                     register_map=w._rmap), add_link=True)
    docks = w.findChildren(QDockWidget)
    for d in docks:
        d.setVisible(True)
    app.processEvents()
    hidden = [d.objectName() for d in docks if not d.isVisible()]
    assert not hidden, f"docks not visible, so the switch does not bind them: {hidden}"
    for i in (0, 1):                                    # first shows build the caches
        w.switch_link(i)
        app.processEvents()
    times = []
    for i in (0, 1, 0, 1, 0, 1, 0, 1):
        t0 = time.perf_counter()
        w.switch_link(i)
        app.processEvents()
        times.append(time.perf_counter() - t0)
    times.sort()
    median, worst = times[len(times) // 2], times[-1]
    slow = sys.platform == "win32" or _SHARED_RUNNER
    ceiling, worst_ceiling = (1.5, 2.4) if slow else (0.75, 1.2)
    record_perf("Link switch", median, ceiling, worst, worst_ceiling)
    assert median < ceiling, (
        f"median Link switch {median * 1000:.0f} ms (ceiling {ceiling * 1000:.0f} ms; "
        f"worst {worst * 1000:.0f} ms). Something on the bind is doing "
        "work proportional to the capture — check the counts in tests/test_links_gui.py "
        "first.")
    assert worst < worst_ceiling, (
        f"worst Link switch {worst * 1000:.0f} ms (ceiling {worst_ceiling * 1000:.0f} ms), "
        f"median {median * 1000:.0f} ms.")

_SAL_WRITER = r"""
import json, sys, zipfile
import numpy as np
from swi3s_studio.ingest import saleae_binary as sb
n, path = int(sys.argv[1]), sys.argv[2]
rng = np.random.default_rng(7)
meta = {"data": {"legacySettings": {"sampleRate": {"digital": 500000000}}},
        "binData": [{"type": "Digital", "file": "digital-0.bin", "deviceChannel": 0},
                    {"type": "Digital", "file": "digital-1.bin", "deviceChannel": 1}]}
with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("meta.json", json.dumps(meta))
    for i in (0, 1):
        e = np.cumsum(rng.integers(1, 40, size=n)).astype(np.uint64)
        z.writestr("digital-%d.bin" % i, sb.build_channel_v3(False, e, chunk_size=65536))
        del e
"""

# SAMPLE CURRENT RSS ACROSS THE LOAD AND TAKE THE MAX. Not `ru_maxrss`, which is a
# high-water mark for the whole PROCESS and therefore measures the load only when the load is
# the biggest thing that process ever did (on some platforms IMPORTING peaks higher than a
# small load). Sampling the CURRENT footprint is immune to anything that happened before the
# load, and it measures precisely what est_peak_bytes models — the peak of the load itself.
_SAL_PEAK_PROBE = r"""
import json, sys, threading, time
try:
    import psutil
except Exception:
    print(json.dumps({"no_psutil": True})); raise SystemExit(0)
from swi3s_studio.ingest import saleae_sal as ss

proc = psutil.Process()
path = sys.argv[1]
cost = ss.estimate_cost(path, channels=[0, 1])
rss = lambda: proc.memory_info().rss
before = rss()
peak = [before]
stop = threading.Event()

def sample():
    while not stop.is_set():
        peak[0] = max(peak[0], rss())
        time.sleep(0.002)

t = threading.Thread(target=sample, daemon=True)
t.start()
cap = ss.load_capture(path, 0, 1, max_bytes=0)
peak[0] = max(peak[0], rss())
stop.set(); t.join(timeout=1.0)
print(json.dumps({"predicted": cost.est_peak_bytes, "before": before, "peak": peak[0],
                  "edges": int(len(cap.clock_edges) + len(cap.data_edges))}))
"""


def test_the_peak_estimate_matches_a_measured_load(tmp_path):
    """est_peak_bytes is a PREDICTION OF RSS; this compares it to a measured RSS.

    Build a .sal (10M transitions per channel), load it whole in a clean subprocess, and
    compare the prediction against that process's own peak RSS. Two bounds, and the
    directions mean different things:
      * predicted >= actual -- the guard must never promise a load will fit and be wrong,
        so the whole-file branch must carry _STREAM_TRANSIENT, not the bare sum.
      * predicted <= 3x actual -- the guard must not refuse loads that would have fitted,
        which is the failure mode of "just multiply everything by a big number".
    """
    import json
    import subprocess
    import sys

    import swi3s_studio

    repo = str(pathlib.Path(swi3s_studio.__file__).resolve().parent.parent)
    env = dict(os.environ, PYTHONPATH=repo)
    sal = str(tmp_path / "measured.sal")
    n = 10_000_000                         # ~0.16 GB of edges: dominates interpreter baseline

    write = subprocess.run([sys.executable, "-c", _SAL_WRITER, str(n), sal],
                           env=env, capture_output=True, text=True)
    assert write.returncode == 0, f"fixture writer failed:\n{write.stderr[-2000:]}"

    def probe(src, label):
        r = subprocess.run([sys.executable, "-c", src, sal],
                           env=env, capture_output=True, text=True)
        assert r.returncode == 0, f"{label} probe failed:\n{r.stderr[-2000:]}"
        return json.loads(r.stdout.strip().splitlines()[-1])

    loaded = probe(_SAL_PEAK_PROBE, "peak")
    if loaded.get("no_psutil"):
        pytest.skip("psutil unavailable, so the load's peak footprint cannot be sampled — "
                    "the model's arithmetic is still covered by "
                    "test_the_peak_estimate_carries_the_decode_transient")
    predicted = int(loaded["predicted"])
    actual = int(loaded["peak"]) - int(loaded["before"])

    assert actual > 100_000_000, (
        f"the load's peak footprint was only {actual / 1e9:.3f} GB "
        f"({int(loaded['before']) / 1e9:.3f} GB before, {int(loaded['peak']) / 1e9:.3f} GB at "
        "peak), close enough to noise that the comparison below is meaningless — either the "
        "fixture needs to be bigger or the load did not happen")
    assert predicted >= actual, (
        f"est_peak_bytes predicted {predicted / 1e9:.3f} GB but the load actually took "
        f"{actual / 1e9:.3f} GB ({actual / predicted:.2f}x). The guard UNDER-predicts, so it "
        "will let a capture through that does not fit. The transient belongs on the edge term "
        "(uncompressed + est_edge_bytes * _STREAM_TRANSIENT).")
    assert predicted <= 3 * actual, (
        f"est_peak_bytes predicted {predicted / 1e9:.3f} GB for a load that took "
        f"{actual / 1e9:.3f} GB ({predicted / actual:.2f}x). Over-predicting refuses captures "
        "that would have opened fine, and the window prompt is not free to the user.")


def test_zoom_and_pan_budget():
    """Wheel-zoom and pan on a timeline ribbon carrying 100k commands.

    What a zoom costs is the ribbon's per-pixel tick decimation: a bisect to the view and a
    vectorised per-column maximum (tests/test_timeline_decimation.py pins the answer). The
    ceilings are cliff detectors that a slow shared runner clears and a per-command Python
    loop does not.
    tests/test_timeline_interaction.py pins by COUNT that a zoom moves no cursor and rebuilds
    no pane, which is the rest.
    """
    import time

    from PySide6.QtCore import QPoint, QPointF, Qt
    from PySide6.QtGui import QWheelEvent
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui.timeline import TimelineRibbon

    app = QApplication.instance() or QApplication([])
    n = 100_000
    r = TimelineRibbon()
    r.resize(1600, TimelineRibbon.BAND_HEIGHT)
    r.show()
    kinds = ("Ping", "WriteA32", "ReadA32", "Commit")
    r.set_events([{"start_sample": i * 1000 + (i * 37) % 500, "command": kinds[i % 4],
                   "bus_row": i} for i in range(n)], n * 1000)
    app.processEvents()

    def wheel(dy=0, dx=0):
        pos = QPointF(r.width() * 0.4, r.height() / 2)
        QApplication.sendEvent(r, QWheelEvent(pos, r.mapToGlobal(pos), QPoint(0, 0),
                                              QPoint(dx, dy), Qt.NoButton, Qt.NoModifier,
                                              Qt.NoScrollPhase, False))
        r.repaint()                                   # the paint is the cost being timed

    wheel(dy=120)                                     # warm the first paint
    times = []
    for kw in [dict(dy=120)] * 6 + [dict(dx=-120)] * 6 + [dict(dy=-120)] * 6:
        t0 = time.perf_counter()
        wheel(**kw)
        times.append(time.perf_counter() - t0)
    times.sort()
    median, worst = times[len(times) // 2], times[-1]
    assert median < 0.025, (
        f"median zoom/pan step {median * 1000:.1f} ms over {n:,} commands (ceiling 25 ms, "
        f"worst {worst * 1000:.1f} ms): the ribbon's tick decimation is "
        "doing per-command work; the decimation must not loop over every command")
    assert worst < 0.05, (
        f"worst zoom/pan step {worst * 1000:.1f} ms (ceiling 50 ms)")


def test_the_widest_line_weight_paints_within_budget(monkeypatch):
    """Preferences ▸ Waveforms lets the Audio and Capture traces be up to 4 px wide, and a
    pen wider than 1 px is stroked far more slowly. Each pane is timed on the full-size demo
    at 4 px, a 1400 px wide paint, median of 7. The cost is a step from 1 px to anything
    wider, not a slope. The ceilings catch a cliff, not drift."""
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.ui import line_style
    from swi3s_studio.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    monkeypatch.setenv("SWI3S_DEMO_SAMPLES", "48000")   # the app's demo, not conftest's 300
    line_style.set_preferences({"weights": {"audio": line_style.MAX_WEIGHT,
                                            "capture": line_style.MAX_WEIGHT}})
    win = MainWindow()
    win.resize(1600, 1000)
    win.show()
    win.load_demo()
    app.processEvents()
    audio, raw = win._audio_view, win._raw_view
    assert {curve.opts["pen"].width() for _p, curve, *_r in audio._plots} == {4}
    assert raw._dp_curve.opts["pen"].width() == 4

    def paint_audio():
        audio.reset_zoom()
        audio._tracks_host.resize(1400, 4 * 130)
        audio._tracks_host.grab()

    def paint_capture():
        raw._plot.setXRange(0, raw._total / raw._rate * 0.02, padding=0)
        raw._plot.resize(1400, 400)
        raw._plot.grab()

    for name, fn, ceiling in (("Audio", paint_audio, 0.08), ("Capture", paint_capture, 0.15)):
        fn()                                           # warm
        times = sorted(_elapsed(fn) for _ in range(7))
        assert times[3] < ceiling, (
            f"{name} paints in {times[3] * 1000:.1f} ms median at 4 px (ceiling "
            f"{ceiling * 1000:.0f} ms)")


def _time_recompute(tv, app, repeats: int = 5) -> tuple:
    """(median, worst) seconds of `repeats` recomputes of `tv`, each asserted to have computed
    margins: a recompute refused (a page still loading, an input the model cannot evaluate)
    returns at once and would time nothing."""
    times = []
    assert not tv._loading, "the Timing page is still loading, so a recompute does nothing"
    for _ in range(repeats):
        tv._rendered = None
        t0 = time.perf_counter()
        tv._recompute()
        app.processEvents()
        times.append(time.perf_counter() - t0)
        assert tv._compute_error == "" and tv._rendered is not None, (
            f"the recompute computed no margins: {tv._compute_error or 'nothing rendered'}")
    times.sort()
    return times[len(times) // 2], times[-1]


def test_timing_recompute_budget():
    """One Timing Calculator recompute: every inequality's worst corner in both columns.

    The corner search evaluates each perturbed row once for all inequalities together
    (`find_worst_corner_rows_all`), searching alpha and delta jointly with the slews and tau,
    and each evaluation runs every leg at each device-supply assignment. The ceilings are
    3.0 s (median) and 4.5 s (worst) on a Mac, and 6.0 s and 9.0 s on Windows and on a shared
    runner (_SHARED_RUNNER). A page with a SoundWire Peripheral is timed
    separately, against its own ceilings of 7.0 s and 10.0 s (13.0 s and 18.0 s on Windows and
    on a shared runner). The `perf margins` section prints the medians.
    """
    from PySide6.QtWidgets import QApplication

    from swi3s_studio.timing.spec_source import is_soundwire
    from swi3s_studio.ui.timing_view import TimingView

    app = QApplication.instance() or QApplication([])
    tv = TimingView()
    tv.show()
    app.processEvents()
    slow = sys.platform == "win32" or _SHARED_RUNNER
    ceiling, worst_ceiling = (6.0, 9.0) if slow else (3.0, 4.5)
    median, worst = _time_recompute(tv, app)
    record_perf("Timing recompute", median, ceiling, worst, worst_ceiling)
    assert median < ceiling, (
        f"median Timing recompute {median * 1000:.0f} ms (ceiling {ceiling * 1000:.0f} ms): "
        "the worst-corner search must evaluate each perturbed row once for all inequalities")
    assert worst < worst_ceiling, (
        f"worst Timing recompute {worst * 1000:.0f} ms (ceiling {worst_ceiling * 1000:.0f} ms)")

    combo = tv._per_spec_combo
    index = combo.findData("SW13_1V2")
    assert index >= 0, "the Peripheral spec list has no SW13_1V2 entry to time"
    combo.setCurrentIndex(index)
    app.processEvents()
    assert is_soundwire(combo.currentData()), "the SoundWire Peripheral page is not selected"
    slow = sys.platform == "win32" or _SHARED_RUNNER
    ceiling, worst_ceiling = (13.0, 18.0) if slow else (7.0, 10.0)
    median, worst = _time_recompute(tv, app)
    record_perf("Timing recompute (SoundWire Peripheral)", median, ceiling, worst, worst_ceiling)
    assert median < ceiling, (
        f"median Timing recompute with a SoundWire Peripheral {median * 1000:.0f} ms "
        f"(ceiling {ceiling * 1000:.0f} ms)")
    assert worst < worst_ceiling, (
        f"worst Timing recompute with a SoundWire Peripheral {worst * 1000:.0f} ms "
        f"(ceiling {worst_ceiling * 1000:.0f} ms)")
    tv.close()
