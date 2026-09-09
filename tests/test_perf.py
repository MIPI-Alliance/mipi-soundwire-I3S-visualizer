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
import pathlib
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
    guards the engine's per-tick placement loop (O(rows x cols x DPs)) + the popcount.

    Speed is NOT a goal of the Python model. swi3score, the C++ core, is the fast
    implementation; swviz/models/dataport.py is a functional model of a hardware
    implementation, published in the MIPI specification, and it optimises for a spec
    reader's comprehension instead. Three hoists that existed here purely for speed
    (a `_num_cols` cached in initialize(), and locals for `_num_channels` in
    clock_tick() and _effective_channel_grouping()) were removed for that reason; they
    cost ~7% on the build below, which is three orders of magnitude clear of the
    ceiling and therefore irrelevant.

    An earlier version of this docstring argued the opposite — it named the three
    hoists and warned that they "read as redundant, so they get removed by
    well-meaning cleanups". That framing sent three successive review drops into the
    same argument. The ceiling below is a runaway guard (an accidental O(n^2), a
    re-decode per tick), not a defence of micro-optimisation.
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
# while the correct O(#edges) path stays sub-second. See the maintainers' performance notes.

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


def test_command_filter_first_keystroke_ceiling():
    """The free-text filter's FIRST sweep after a model reset, which is the expensive one.

    QSortFilterProxyModel re-tests every source row on the first invalidateFilter, and the
    row-text cache is cold after set_commands/set_peripheral_maps — so the first character
    typed into Filter ▸ Expression renders the whole table on the GUI thread. It used to do
    that through self.data(self.index(row, col)) for all twelve columns: 0.58 s at 10k
    commands, 2.9 s at 50k and 11.5 s at 200k, recurring after every load, re-decode and
    Clear All Filters. Building the same string via _display_text (no QModelIndex, no role
    dispatch) is ~10x cheaper.

    A CEILING, NOT A BENCHMARK: generous enough for a loaded runner, tight enough to catch
    the index+role path being reintroduced — that would blow 6 s at 50k rows.
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
        "Rebuilding the row cache through data()/index() per cell costs ~2.9s here and "
        "~11.5s at 200k — on the GUI thread.")


def test_guard_suppressing_build_stays_linear_in_rows():
    """A build whose data writes suppress their own guards, once per row.

    `remove_bits_matching` used to rebuild the whole `bits` list per call, so this shape went
    QUADRATIC in the row count: 0.21 s at 500 rows, 2.18 s at 2000, 36.59 s at 8000 — 4x the
    rows costing 17x the time. Deferring the removals to one `compact()` makes it linear
    (0.10 / 0.43 / 2.00 s).

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
            "per-removal rebuild of bus_model.bits looks to be back")
    finally:
        logging.disable(logging.NOTSET)


def test_low_entropy_locate_ceiling():
    """The tight wall-clock ceiling for the low-entropy sub-capture search.

    A constant (never-toggling) sub-pattern makes almost every offset in the main capture an
    exact match, which without a cap on collected hits is O(N * hits) and stalled the UI for
    minutes. tests/test_subcapture.py keeps a LOOSE bound in the default suite — a hang is
    worth catching on every config — and the ceiling that would actually notice a slowdown
    lives here, where runner jitter cannot redden an ordinary run.
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
    sub_no_match = Capture(clock_edges=sub_clk, data_edges=sub_clk,
                           initial_clock=False, initial_data=True,
                           sample_rate_hz=1_000_000)

    def both():
        locate_subcapture(main, sub)              # the exact-match path (capped)
        locate_subcapture(main, sub_no_match)     # the seed path (no matches)

    dt = _elapsed(both)
    assert dt < 15.0, f"low-entropy locate took {dt:.2f}s (ceiling 15s) — perf regression?"


def test_decoded_samples_rebuild_ceiling():
    """A cursor move that leaves the Samples window rebuilds the pane — bound that rebuild.

    The Decoded Samples pane is a QTableWidget refilled WHOLESALE, not a virtual model, so
    re-centring costs 2 * _SAMPLE_HALF_WINDOW * len(_COLS) fresh QTableWidgetItems. At the
    original half-window of 4000 that is 48,000 items, which profiled at ~99 ms of a 142 ms
    cursor move on a 2520x1350 dpr-2.0 display — the whole of a reported "the cursor jumps
    instantly, then the app hangs for a moment" complaint, because jumping is precisely what
    leaves the window. Hiding the dock took the same move to 43 ms, which is what identified
    this pane rather than the decode.

    Two things can redden this: raising _SAMPLE_HALF_WINDOW back up, or making _fill_row more
    expensive per cell. Both are the same defect from the user's side. The ceiling is loose
    because offscreen Qt elides the per-item layout that dominates on a real display — it is
    here to catch the order of magnitude, and 8000 rows would need ~9x the budget.
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
    the constant IS the freeze a cursor move can cause. It was 32 M with a comment claiming
    "sub-~0.3s"; the scan actually measures ~32 ns/UI (19.2 M UIs of a real 4.7 s capture
    took 616 ms), so 32 M admitted ~1.0 s. A 4.7 s capture's own region fell under the
    threshold, which made the first cursor move into it a ~550 ms freeze with TX
    persistence on -- 530 ms median measured against 28 ms with it off.

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
    """The WHOLE cursor cascade, with every dock open — the thing users actually feel.

    Every other ceiling here times one function. The complaint is always "moving the cursor
    takes half a second", and that is a cascade across ten docks: raw view, register replay,
    command selection, CDS symbols, Decoded Samples, and the bus grid. No test drove it, so
    the 99 ms-per-move Samples pane and the ~550 ms TX-persistence freeze both shipped
    green, and the numbers that found them (28 ms with persistence off, 47 ms median under
    cProfile with ten docks visible, 530 ms with it on) lived only in commit messages.

    Measured on the reported capture, per move: 142 ms median before 3.0.17, 57 ms after.

    THE FIXTURE IS THE POINT. It was from_demo(600) -- 314 k UIs -- and at that size a
    capture-PROPORTIONAL per-move cost barely registers: the Samples-pane defect that cost
    ~99 ms on the real 4.7 s capture adds only ~19 ms there, so no ceiling loose enough to
    survive a shared runner could have caught the very bug this test exists for.
    from_demo(20000) costs ~1.9 s to build and lands at 10.2 M UIs / 408 commands / 3 config
    regions, within a factor of two of the reported capture (19.2 M UIs, 423 commands, 2
    regions) on every axis that matters. The same defect is 29.8 ms -> 94.7 ms median on it.

    Baseline here is 29.8-31.0 ms median / 38.5-38.8 ms worst, stable to ~1 ms across runs,
    so the ceilings sit at ~4x and ~6x. That headroom is for a slower runner, not for drift:
    a 3x slower machine still comes in around 90/115 ms.

    What this deliberately does NOT chase is a KNOWN constant being raised. The pane
    regression above reaches ~95 ms and passes the 120 ms ceiling on purpose -- it is caught
    absolutely and deterministically by _SAMPLE_HALF_WINDOW <= 1000 in
    test_decoded_samples_rebuild_ceiling and by the counts in tests/test_cursor_cost.py. A
    wall clock's job here is the cost nobody has bounded yet: a new scan on the cascade, a
    pane that stopped checking its dock, an O(capture) call added to a paint path. Splitting
    it that way is what lets this ceiling stay loose enough to be trustworthy.
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
    assert shown >= 5, f"only {shown}/{len(docks)} docks visible — the cascade is not loaded"
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
    assert median < 0.12, (
        f"median cursor move {median * 1000:.0f} ms across {shown} visible docks over "
        f"{len(ce):,} UIs (ceiling 120 ms, baseline ~30 ms; worst {worst * 1000:.0f} ms). "
        "Something on the cascade is doing work proportional to the capture on the GUI "
        "thread — see tests/test_cursor_cost.py for which counts to check first.")
    assert worst < 0.25, (
        f"worst cursor move {worst * 1000:.0f} ms (ceiling 250 ms, baseline ~39 ms), median "
        f"{median * 1000:.0f} ms. A single move stalling while the rest are fast is a COLD "
        "path — a first-visit scan running inline instead of on a worker "
        "(_TX_PERSIST_SYNC_MAX_UIS) is the shape that did this before.")


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
# the biggest thing that process ever did. Three attempts at this, and the two that used
# maxrss were both wrong on a platform the author was not looking at:
#   * fixture built in the same process -> baseline already huge; reported 0.164 GB for a load
#     that truly took 0.580 GB, and would have passed a 3x under-prediction;
#   * `maxrss` after minus `maxrss` before, in one process -> exactly 0 on a second test
#     platform, where IMPORTING peaks at ~0.9 GB and then releases it, so a 0.2 GB load never
#     exceeds the earlier transient. Subtracting two such processes does not help either:
#     they share that same import transient.
# Sampling the CURRENT footprint is immune to anything that happened before the load, and it
# measures precisely what est_peak_bytes models — the peak of the load itself.
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
    """est_peak_bytes is a PREDICTION OF RSS, and nothing compared it to an RSS.

    That gap is why a 291 MB capture predicted at 20.29 GB actually reached ~60 GB on a
    103 GB machine: _STREAM_TRANSIENT was applied only to the windowed branch, so the
    whole-file branch under-predicted by 1.9x-3.6x on measured captures. The model was then
    recalibrated by hand against three real captures -- which nothing preserves, so it can
    drift straight back.

    This closes the loop: build a .sal, load it whole in a clean subprocess, and compare the
    prediction against that process's own peak RSS. Two bounds, and the directions mean
    different things:
      * predicted >= actual -- the guard must never promise a load will fit and be wrong.
        This is the assertion that catches the 60 GB defect: reverting to the bare sum
        predicts 0.072 GB where this fixture measures 0.200 GB, a 2.78x under-prediction on
        a fixture ~100x smaller than the one that hurt.
      * predicted <= 3x actual -- the guard must not refuse loads that would have fitted,
        which is the failure mode of "just multiply everything by a big number". A transient
        of 16 instead of 4 trips this at 5.00x.
    Measured pred/actual: 1.27-1.37 across 3M-12M transitions here, and 1.00-1.25 on the
    three real captures the model was fitted to.
    """
    import json
    import subprocess
    import sys

    import swi3s_studio

    repo = str(pathlib.Path(swi3s_studio.__file__).resolve().parent.parent)
    env = dict(os.environ, PYTHONPATH=repo)
    sal = str(tmp_path / "measured.sal")
    n = 4_000_000                          # ~0.2 GB of edges: dominates interpreter baseline

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
        "will let a capture through that does not fit — that is how a 20.29 GB prediction "
        "reached ~60 GB resident. The transient belongs on the edge term "
        "(uncompressed + est_edge_bytes * _STREAM_TRANSIENT).")
    assert predicted <= 3 * actual, (
        f"est_peak_bytes predicted {predicted / 1e9:.3f} GB for a load that took "
        f"{actual / 1e9:.3f} GB ({predicted / actual:.2f}x). Over-predicting refuses captures "
        "that would have opened fine, and the window prompt is not free to the user.")
