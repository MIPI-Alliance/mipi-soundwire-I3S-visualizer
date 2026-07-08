"""End-to-end PHY bring-up GUI test: loading a Cold Start capture shows
'No PHY Selected' until the §5.1.2 PHY-select sequence is decoded, the register
view highlights the active PHY, the timeline marks the bring-up region, and a
real capture lands in Analysis mode.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 tests/test_phy_bringup_gui.py
"""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from swi3s_studio.ui.main_window import MainWindow
from swi3s_studio.ui.mode_controller import ANALYSIS, VISUALIZATION
from swi3s_studio.session import Session
from swi3s_studio.ingest import transitions


def test_bringup_demo_gates_grid_and_highlights_phy():
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_demo_bringup()
    s = win._session

    # The capture has an observable Cold Start selecting PHY2.
    assert s.has_bringup and s.link_control.sequence == "cold"
    assert s.link_control.phy_name == "PHY2"
    assert s.audio_start_sample > 0

    # At load (cursor 0, inside the bring-up) the grid shows the 'No PHY Selected'
    # message rather than a configured bus geometry.
    win.cursor.set_sample(0)
    texts = [it.toPlainText() for it in win._grid_view._scene.items()
             if hasattr(it, "toPlainText")]
    assert any("No PHY Selected" in t for t in texts), texts
    print("ok: grid gated — 'No PHY Selected' during bring-up")

    # Past audio_start the grid renders real cells (geometry exists again).
    win.cursor.set_sample(s.commands[-1]["start_sample"])
    assert win._grid_view.item_count >= s.column_count
    print(f"ok: grid renders after audio start ({win._grid_view.item_count} items)")

    # Register view marks PHY2 active; timeline knows the bring-up region.
    assert win._reg_view._active_phy == "PHY2"
    assert win._timeline._bringup and win._timeline._bringup["audio_start"] == s.audio_start_sample
    assert win._timeline._bringup["phy_select"] is not None
    print("ok: register view active PHY = PHY2; timeline bring-up marker set")


def test_real_capture_lands_in_analysis_and_gates():
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    # A non-demo capture source (like an opened .bin) with a Cold Start spliced in.
    cap = transitions.prepend_cold_start(transitions.demo_capture(64), phy_number=2)
    sess = Session(cap, register_map=win._rmap)          # source defaults to "capture"
    assert sess.source.get("type") != "demo"
    win.load_session(sess)

    # Opening a real capture lands in Analysis mode (where the decoded grid lives),
    # and the grid is gated at the t=0 cursor.
    assert win._mode_mgr.current() == ANALYSIS, win._mode_mgr.current()
    win.cursor.set_sample(0)
    texts = [it.toPlainText() for it in win._grid_view._scene.items()
             if hasattr(it, "toPlainText")]
    assert any("No PHY Selected" in t for t in texts), texts
    # Switching to Visualization and back to Analysis keeps the gating (the mode
    # handler must not re-show the final grid over the t=0 cursor).
    win._mode_mgr.switch_to(VISUALIZATION)
    win._mode_mgr.switch_to(ANALYSIS)
    texts = [it.toPlainText() for it in win._grid_view._scene.items()
             if hasattr(it, "toPlainText")]
    assert any("No PHY Selected" in t for t in texts), "mode switch un-gated the grid"
    print("ok: real capture → Analysis mode, grid gated, stays gated across mode switch")


def test_plain_demo_unchanged():
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    # The menu's Load Demo now includes the Cold Start bring-up; the plain (no
    # bring-up) demo is still available as a Session and must behave as before.
    win.load_session(Session.from_demo(256, register_map=win._rmap))
    # No bring-up: opening a capture lands in Analysis (like any capture), there's no
    # timeline bring-up band, and at the t=0 cursor the decoded grid has no active data
    # ports (config not yet applied).
    assert not win._session.has_bringup
    assert win._mode_mgr.current() == ANALYSIS
    assert win._timeline._bringup == {}
    win.cursor.set_sample(0)
    assert not win._grid_view._stream_colors, "no data ports at t=0"
    print("ok: plain demo (no bring-up) — lands in Analysis, no band, gated at t=0")


def test_timeline_safe_lock_band_labelled_by_phy():
    """The initial audio segment of a cold/warm start is the selected PHY's Safe-Lock
    geometry, so the timeline labels that band by sequence + PHY (e.g. 'Cold Start
    PHY2'), not the bare '{cols}col'. Later segments keep the plain column label. Also
    checks the ribbon carries NO widget tooltip (it obscured the marks)."""
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    tl = win._timeline
    # Synthetic bring-up: cold start, PHY2, Safe-Lock-2.
    tl._bringup = {"start": 0, "phy_select": None, "phystart": None, "audio_start": 500,
                   "label": "PHY2", "sequence": "cold", "phy_name": "PHY2",
                   "safe_lock_columns": 2}
    assert tl._segment_label(0, 2) == "Cold Start PHY2", tl._segment_label(0, 2)
    assert tl._segment_label(1, 16) == "16col"           # later operational band unchanged
    # A different PHY / warm start reflects in the label.
    tl._bringup.update(sequence="warm", phy_name="PHY3", safe_lock_columns=4)
    assert tl._segment_label(0, 4) == "Warm Start PHY3"
    # No bring-up -> plain column label.
    tl._bringup = {}
    assert tl._segment_label(0, 2) == "2col"
    assert not tl.toolTip(), "timeline should carry no obscuring widget tooltip"


if __name__ == "__main__":
    test_bringup_demo_gates_grid_and_highlights_phy()
    test_real_capture_lands_in_analysis_and_gates()
    test_plain_demo_unchanged()
    test_timeline_safe_lock_band_labelled_by_phy()
    print("ok: timeline safe-lock band labelled by PHY (Cold/Warm Start PHYn) + no tooltip")
    print("PHY BRING-UP GUI TEST PASSED")
