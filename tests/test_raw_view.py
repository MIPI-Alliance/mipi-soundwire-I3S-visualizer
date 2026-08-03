"""Raw Capture pane regressions.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_raw_view.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
from PySide6.QtWidgets import QApplication

from swi3s_studio.ui.raw_view import RawCaptureView


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
    (all sources share) stays full-height. Regression for a full-bandwidth
    right-channel capture."""
    import os as _os
    import tempfile as _tf

    from swi3s_studio.model import viz_engine
    from swi3s_studio.model.bus_config import CDS_GUARD_G0, BusConfig
    from swi3s_studio.ui.grid_view import GridView

    def _guard_labels(guard):
        cfg = BusConfig(num_columns=8, row_rate_khz=3072.0); cfg.cds_bit_width = 0
        cfg.cds_guard = list(guard); cfg.enforce_cds_handover = False
        p = _tf.mktemp(suffix=".csv"); open(p, "w").write(cfg.to_csv())
        cells, _cl, _i, ncols, nr = viz_engine.render_payload(p); _os.remove(p)
        split = any(c.get("cds_split") for c in cells)
        gv = GridView(); gv.set_bus_model(cells, {}, ncols, 1)
        # Grid cell labels are QGraphicsSimpleTextItem (.text()); a non-grid centred
        # message would be QGraphicsTextItem (.toPlainText()). Collect either kind.
        txt = [(it.text() if hasattr(it, "text") else it.toPlainText())
               for it in gv._scene.items() if hasattr(it, "text") or hasattr(it, "toPlainText")]
        return split, {t for t in txt if t in ("G0", "M G0")}

    QApplication.instance() or QApplication([])
    # Divergent: Manager G0, all devices off -> cds_split True -> "M G0".
    split, labels = _guard_labels([CDS_GUARD_G0] + [0] * 12)
    assert split is True and "M G0" in labels and "G0" not in labels, (split, labels)
    # Uniform: all sources G0 -> not split -> full-height "G0".
    split, labels = _guard_labels([CDS_GUARD_G0] * 13)
    assert split is False and "G0" in labels and "M G0" not in labels, (split, labels)


def test_click_snaps_to_nearby_cds_commit_marker():
    """A Raw Capture click within ~1 UI of a shown CDS/Commit Row-Sync-Point marker
    snaps to that marker's exact RSP sample, so the cursor lands ON the RSP-anchored
    commit ring / CDS triangle (otherwise a few-sample miss drifts from them as you zoom
    in). Clicks away from markers keep the exact sample; no snap when markers are hidden."""
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
