"""Raw Capture pane: lane order, CDS guard labels, marker snapping and edge drawing.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_raw_view.py
"""
import os
import pathlib

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest
from PySide6.QtWidgets import QApplication

from swi3s_studio.ui.raw_view import RawCaptureView

pytestmark = pytest.mark.gui


class _Cap:
    def __init__(self):
        self.clock_edges = np.arange(0, 1000, 2, dtype=np.uint64)
        self.data_edges = np.arange(1, 1000, 3, dtype=np.uint64)
        self.initial_clock = False
        self.initial_data = False
        self.sample_rate_hz = 1000


def test_clock_always_on_bottom_across_fbcse_flip():
    """The forwarded clock is ALWAYS the bottom trace (_dn_curve) and the data the top
    (_dp_curve), whichever physical line (DP/DN) carries the clock — so 'clock on the
    bottom' holds across an FBCSE flip, Hide Clock only ever hides the bottom curve
    (never the data), and the physical name labelling each trace swaps with orientation."""
    QApplication.instance() or QApplication([])
    rv = RawCaptureView()
    rv.set_capture(_Cap(), dp_is_data_line=False)   # clock = DP (physically)
    assert (rv._bot_name, rv._top_name) == ("DP", "DN"), (rv._bot_name, rv._top_name)
    rv._clock_btn.setChecked(False)                  # Hide Clock -> hides the bottom (clock) curve
    assert rv._dp_curve.isVisible() and not rv._dn_curve.isVisible()

    rv.set_capture(_Cap(), dp_is_data_line=True)      # flip: clock = DN, data = DP
    assert (rv._bot_name, rv._top_name) == ("DN", "DP"), (rv._bot_name, rv._top_name)
    assert rv._dp_curve.isVisible(), "data curve (top) hidden after FBCSE flip"
    assert not rv._dn_curve.isVisible(), "clock curve (bottom) should stay hidden"

    rv._clock_btn.setChecked(True)                    # Show Clock -> both visible
    assert rv._dp_curve.isVisible() and rv._dn_curve.isVisible()


def test_cds_peripheral_guard_label_uses_present_devices():
    """The CDS split-cell peripheral (P) guard label aggregates the devices PRESENT on
    the bus (any device>=0 driving data/guard/tail — self._present_periph): a shared
    polarity reads "P G0"/"P G1", disagreeing guard polarities read "P Gx", and a
    present peripheral that ISN'T guarding the column (it hands over) makes it "P Mix".
    The Manager band is a single source, so it's just its polarity. See _cds_guard_core."""
    from swi3s_studio.ui.grid_view import _G0, _G1, GridView
    QApplication.instance() or QApplication([])
    gv = GridView()

    # Manager band: single source, never "Mix"/"Gx".
    assert gv._cds_guard_core({-1: _G0}, is_mgr=True) == "G0"
    assert gv._cds_guard_core({-1: _G1}, is_mgr=True) == "G1"

    # Peripheral band, only Dev0 present and guarding G1 -> "G1".
    gv._present_periph = {0}
    assert gv._cds_guard_core({0: _G1}, is_mgr=False) == "G1"
    # Dev1 present (e.g. owns a data port) but not guarding this column -> "Mix".
    gv._present_periph = {0, 1}
    assert gv._cds_guard_core({0: _G1}, is_mgr=False) == "Mix"
    # Both present and guarding, differing polarity -> "Gx"; agreeing -> "G1".
    assert gv._cds_guard_core({0: _G1, 1: _G0}, is_mgr=False) == "Gx"
    assert gv._cds_guard_core({0: _G1, 1: _G1}, is_mgr=False) == "G1"


def test_cds_manager_only_guard_splits_even_without_peripheral_cells():
    """A divergent CDS where only the Manager drives a guard (devices off, no handover)
    emits a single Manager guard cell — cell-shape-identical to the uniform case — so it
    must split ("M G0") on the engine's `cds_split` flag, not full-height "G0". Uniform
    (all sources share) stays full-height."""
    import os as _os
    import tempfile as _tf

    from swi3s_studio.model import viz_engine
    from swi3s_studio.model.bus_config import BusConfig
    from swi3s_studio.ui.grid_view import GridView

    def _guard_labels(guard):
        cfg = BusConfig(num_columns=8, row_rate_khz=3072.0); cfg.cds_bit_width = 0
        cfg.cds_guard_enable = list(guard); cfg.enforce_cds_handover = False
        with _tf.TemporaryDirectory() as d:
            p = _os.path.join(d, "guard.csv"); pathlib.Path(p).write_text(cfg.to_csv())
            cells, _cl, _i, ncols, nr = viz_engine.render_payload(p)
        split = any(c.get("cds_split") for c in cells)
        gv = GridView(); gv.set_bus_model(cells, {}, ncols, 1)
        # Grid cell labels are QGraphicsSimpleTextItem (.text()); a non-grid centred
        # message would be QGraphicsTextItem (.toPlainText()). Collect either kind.
        txt = [(it.text() if hasattr(it, "text") else it.toPlainText())
               for it in gv._scene.items() if hasattr(it, "text") or hasattr(it, "toPlainText")]
        return split, {t for t in txt if t in ("G0", "M G0")}

    QApplication.instance() or QApplication([])
    # Divergent: Manager G0, all devices off -> cds_split True -> "M G0".
    split, labels = _guard_labels([True] + [False] * 12)
    assert split is True and "M G0" in labels and "G0" not in labels, (split, labels)
    # Uniform: all sources G0 -> not split -> full-height "G0".
    split, labels = _guard_labels([True] * 13)
    assert split is False and "G0" in labels and "M G0" not in labels, (split, labels)


def test_click_snaps_to_nearby_cds_commit_marker():
    """A Raw Capture click within ~1 UI of a shown CDS/Commit Row-Sync-Point marker
    snaps to that marker's exact RSP sample, so the cursor lands ON the RSP-anchored
    commit ring / CDS triangle. Clicks away from markers keep the exact sample; no snap
    when markers are hidden."""
    import numpy as np
    QApplication.instance() or QApplication([])
    rv = RawCaptureView()
    rv._show_cds = True
    rv._ui_samples = 4.0
    rv._commit_samples = np.array([1000], dtype=np.int64)
    rv._cds_samples = np.array([1000, 1032, 1064], dtype=np.int64)
    assert rv._snap_click(1002) == 1000          # within 1 UI of a marker -> snap to its RSP
    assert rv._snap_click(1034) == 1032          # snaps to the nearest marker
    assert rv._snap_click(1020) == 1020          # >1 UI from any marker -> exact (fine pos)
    rv._show_cds = False
    assert rv._snap_click(1002) == 1002          # markers hidden -> no snap


def test_a_stopped_line_draws_its_true_level_at_every_zoom():
    """Zoomed out, the edges are cut to what can be drawn, and a stretch where the line has
    stopped draws its true level whatever the zoom and scroll."""
    from swi3s_studio.nputil import searchsorted
    from swi3s_studio.ui.raw_view import _decimate_edges, _step_xy_from
    view = RawCaptureView._MAX_EDGES
    before = np.arange(0, 600_001, 2, dtype=np.uint64)     # an odd count: stops high
    after = np.arange(1_800_000, 2_400_000, 2, dtype=np.uint64)
    edges = np.concatenate([before, after])
    stopped = (int(before[-1]) + int(after[0])) // 2
    truth = bool(before.size & 1)
    assert truth, "the stop should be high, so a held low shows"
    wrong = []
    for width in (300_000, 700_000, 1_250_000, 1_700_000, 2_000_000, 2_400_000):
        for shift in (0, 37_001, 120_003, -90_001):
            lo = max(0, stopped - width // 2 + shift)
            hi = stopped + width // 2 + shift
            i0, i1 = int(searchsorted(edges, lo)), int(searchsorted(edges, hi))
            win = edges[i0:i1]
            if win.size > view:
                win = _decimate_edges(win, lo, hi, view // 3)
                assert win.size <= view and win.dtype == np.uint64
            xs, ys = _step_xy_from(win, bool(i0 & 1), lo, hi, 0.0, 1.0)
            k = int(np.searchsorted(np.asarray(xs), stopped, side="right")) - 1
            if (ys[k] > 0.5) != truth:
                wrong.append((width, shift))
    assert not wrong, f"the stopped line drew the wrong level at (width, shift) {wrong}"


def test_the_edge_reduction_never_copies_the_window():
    """The capture's edges are uint64 and a window can hold hundreds of millions of them:
    searching it with float bounds would make numpy copy the whole window to float64 on
    every render. What the reduction allocates is bounded by its slots, not the window."""
    import tracemalloc

    from swi3s_studio.ui.raw_view import _decimate_edges
    win = np.arange(3, 3 + 2 * 4_000_000, 2, dtype=np.uint64)      # 32 MB
    lo, hi = 0, int(win[-1]) + 1
    tracemalloc.start()
    try:
        tracemalloc.reset_peak()
        kept = _decimate_edges(win, lo, hi, 20_000)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert kept.dtype == np.uint64 and 0 < kept.size <= 60_000
    assert peak < win.nbytes // 8, f"{peak} bytes allocated for a {win.nbytes}-byte window"


def _port_view(provider):
    QApplication.instance() or QApplication([])
    rv = RawCaptureView()
    rv.set_capture(_Cap(), dp_is_data_line=False)
    rv.set_port_marks(provider, [(0, 1, 0), (0, 1, 1)])
    rv._samp_btn.setChecked(True)
    return rv


def _marks(*lanes):
    """Port marks for the given channels: two words each, the first bit of each an MSB."""
    rows = [(s, ch, st) for ch in lanes for s, st in ((100, True), (110, False),
                                                      (300, True), (310, False))]
    return {"sample": [r[0] for r in rows], "device": [0] * len(rows), "dp": [1] * len(rows),
            "channel": [r[1] for r in rows], "is_start": [r[2] for r in rows]}


def _shown(rv, key):
    return [lbl.textItem.toPlainText() for lbl in rv._lane_labels[key] if lbl.textItem.toPlainText()]


def test_a_lane_with_no_marks_in_the_window_shows_no_channel_labels():
    """A lane that has no sampled bits in the window clears its CHn labels with its dots
    while another lane still draws."""
    state = {"lanes": (0, 1)}
    rv = _port_view(lambda lo, hi: _marks(*state["lanes"]))
    assert _shown(rv, (0, 1, 0)) == ["CH0", "CH0"] and _shown(rv, (0, 1, 1)) == ["CH1", "CH1"]
    state["lanes"] = (1,)
    rv._refresh_ports()
    assert _shown(rv, (0, 1, 0)) == []
    assert _shown(rv, (0, 1, 1)) == ["CH1", "CH1"]


def test_retheme_recolours_the_recovered_clock_trace():
    """The recovered bit clock trace takes the palette's text colour after a switch."""
    from swi3s_studio.ui.theme import VizTheme, apply_palette
    QApplication.instance() or QApplication([])
    apply_palette("dark")
    rv = RawCaptureView()
    try:
        apply_palette("light")
        rv.retheme()
        assert rv._rec_clk_curve.opts["pen"].color().name() == VizTheme.TEXT.lower()
    finally:
        apply_palette("dark")


class _Click:
    def __init__(self, scene_pos):
        self._p = scene_pos

    def button(self):
        from PySide6.QtCore import Qt
        return Qt.LeftButton

    def scenePos(self):
        return self._p

    def accept(self):
        pass


def _shown_raw_view():
    QApplication.instance() or QApplication([])
    rv = RawCaptureView()
    rv.resize(900, 400)
    rv.show()
    rv.set_capture(_Cap(), dp_is_data_line=False)
    QApplication.processEvents()
    return rv


def test_a_click_in_the_pad_past_the_end_seeks_to_the_last_sample():
    """The blank pad right of the capture is not a position: a click there seeks to the
    capture's last sample, never past it."""
    from PySide6.QtCore import QPointF
    rv = _shown_raw_view()
    got = []
    rv.sampleSelected.connect(got.append)
    vb = rv._plot.getViewBox()
    x_end = rv._total / rv._rate
    assert vb.viewRange()[0][1] > x_end                 # the pad is on screen
    box = vb.sceneBoundingRect()
    p = QPointF(box.right() - 3, box.center().y())
    assert vb.mapSceneToView(p).x() > x_end
    rv._on_click(_Click(p))
    assert got == [rv._total]


def test_a_click_on_an_axis_seeks_nowhere():
    """A click left of the data area (the axis) moves nothing."""
    from PySide6.QtCore import QPointF
    rv = _shown_raw_view()
    got = []
    rv.sampleSelected.connect(got.append)
    vb = rv._plot.getViewBox()
    left = vb.sceneBoundingRect().left()
    rv._on_click(_Click(QPointF(left - 10, vb.sceneBoundingRect().center().y())))
    assert got == []


def test_step_arrays_hold_each_level_up_to_its_edge():
    """A step trace is the window start, then each edge twice (hold, transition), then the
    window end, at the level before the edge and the level after it; it comes back as arrays."""
    from swi3s_studio.ui.raw_view import _step_xy_from
    win = np.array([10, 20, 35], dtype=np.uint64)
    for init in (False, True):
        xs, ys = _step_xy_from(win, init, 0, 50, 0.0, 1.0)
        assert isinstance(xs, np.ndarray) and isinstance(ys, np.ndarray)
        assert xs.tolist() == [0, 10, 10, 20, 20, 35, 35, 50]
        a, b = (1.0, 0.0) if init else (0.0, 1.0)
        assert ys.tolist() == [a, a, b, b, a, a, b, b]
    xs, ys = _step_xy_from(np.array([], dtype=np.uint64), True, 5, 9, 0.0, 1.0)
    assert xs.tolist() == [5, 9] and ys.tolist() == [1.0, 1.0]


def test_a_hidden_clock_is_not_built_until_it_is_shown_again():
    """The bottom band is built only while it is drawn: hiding the clock skips its traces,
    and showing it renders the current window."""
    QApplication.instance() or QApplication([])
    rv = RawCaptureView()
    rv.set_capture(_Cap(), dp_is_data_line=True)
    rv._clock_btn.setChecked(False)
    rv._dn_curve.setData([], [])
    rv._render_range(0, 1000)
    assert rv._dn_curve.getData()[0] is None or len(rv._dn_curve.getData()[0]) == 0
    assert rv._dp_curve.getData()[0] is not None and len(rv._dp_curve.getData()[0]) > 2
    rv._clock_btn.setChecked(True)
    assert len(rv._dn_curve.getData()[0]) > 2


def test_a_capture_with_no_transitions_clears_the_previous_capture_trace():
    """The pane is reused when the active Link changes: binding a capture whose lines never
    toggle leaves no trace from the one before it."""
    QApplication.instance() or QApplication([])
    rv = RawCaptureView()
    rv.set_capture(_Cap(), dp_is_data_line=True)
    assert len(rv._dp_curve.getData()[0]) > 2 and len(rv._dn_curve.getData()[0]) > 2
    empty = _Cap()
    empty.clock_edges = np.array([], dtype=np.uint64)
    empty.data_edges = np.array([], dtype=np.uint64)
    rv.set_capture(empty, dp_is_data_line=True)
    for curve in (rv._dp_curve, rv._dn_curve, rv._rec_clk_curve):
        xs = curve.getData()[0]
        assert xs is None or len(xs) == 0
