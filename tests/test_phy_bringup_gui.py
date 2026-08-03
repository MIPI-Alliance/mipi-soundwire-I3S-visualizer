"""End-to-end PHY bring-up GUI test: loading a Cold Start capture shows
'No PHY Selected' until the §5.1.2 PHY-select sequence is decoded, the register
view highlights the active PHY, the timeline marks the bring-up region, and a
real capture lands in Analysis mode.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_phy_bringup_gui.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from swi3s_studio.ingest import transitions
from swi3s_studio.session import Session
from swi3s_studio.ui.main_window import MainWindow
from swi3s_studio.ui.mode_controller import ANALYSIS, VISUALIZATION


def test_bringup_demo_gates_grid_and_highlights_phy():
    _app = QApplication.instance() or QApplication([])
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
    _app = QApplication.instance() or QApplication([])
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


def test_demo_cold_start_is_spec_valid():
    _app = QApplication.instance() or QApplication([])
    win = MainWindow()
    # The demo the app loads synthesises a valid §5.1.2 Cold Start selecting PHY2 (the
    # app preloads with cold_start=True; see MainWindow._init_ui / load_demo). It starts
    # in safe-lock-2 and commits up, so the bring-up is a real Cold Start — not the
    # coalesced-audio false positive a bare audio capture would trip.
    win.load_session(Session.from_demo(256, cold_start=True, register_map=win._rmap))
    s = win._session
    assert s.has_bringup and s.link_control.sequence == "cold"
    assert s.link_control.phy_name == "PHY2"
    assert win._mode_mgr.current() == ANALYSIS
    # Every observable §5.2.3 Link-Control timing is within spec, including the two-part
    # Bus Reset: Man_tReset00 (212–236 µs) then Man_tReset10 (≥1569 µs).
    rows = {t["param"]: t for t in (s.link_control.timing or [])}
    assert rows and all(t["ok"] for t in rows.values()), rows
    assert 212.0 <= rows["Man_tReset00"]["measured_us"] <= 236.0, rows["Man_tReset00"]
    print("ok: demo synthesises a spec-valid Cold Start selecting PHY2")


def test_timeline_safe_lock_band_labelled_by_phy():
    """The initial audio segment of a cold/warm start is the selected PHY's Safe-Lock
    geometry, so the timeline labels that band by the PHY (e.g. 'PHY2 Safe-Lock-2'),
    not the bare '{cols}col'. It is deliberately NOT called 'Cold Start' — the
    cold-start SEQUENCE is the link-control region drawn before the audio band. Later
    segments keep the plain column label. Also checks the ribbon carries NO widget
    tooltip (it obscured the marks)."""
    _app = QApplication.instance() or QApplication([])
    win = MainWindow()
    tl = win._timeline
    # Synthetic bring-up: cold start, PHY2, Safe-Lock-2.
    tl._bringup = {"start": 0, "phy_select": None, "phystart": None, "audio_start": 500,
                   "label": "PHY2", "sequence": "cold", "phy_name": "PHY2",
                   "safe_lock_columns": 2}
    assert tl._segment_label(0, 2) == "PHY2 Safe-Lock-2", tl._segment_label(0, 2)
    assert tl._segment_label(1, 16) == "16col"           # later operational band unchanged
    # A different PHY reflects in the label; still no "Cold/Warm Start" in the audio band.
    tl._bringup.update(sequence="warm", phy_name="PHY3", safe_lock_columns=4)
    assert tl._segment_label(0, 4) == "PHY3 Safe-Lock-4"
    # No bring-up -> plain column label.
    tl._bringup = {}
    assert tl._segment_label(0, 2) == "2col"
    assert not tl.toolTip(), "timeline should carry no obscuring widget tooltip"


def test_grid_message_never_claims_phy_before_it_is_clocked():
    """The bring-up grid overlay must not name a PHY before its number is clocked
    (PhyStart). Before that: 'No PHY Selected' with a neutral sub-phase subtext (no PHY
    claim, since it can't be known from the wire at that cursor). After PhyStart but
    before audio: 'PHYn Selected'. Once audio starts: the real grid renders."""
    _app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_session(Session.from_demo(4000, cold_start=True))
    lc = win._session.link_control
    assert lc.sequence == "cold" and lc.phy_name == "PHY2"

    def msgs():
        return [it.toPlainText() for it in win._grid_view._scene.items()
                if hasattr(it, "toPlainText")]

    # Deep in the Bus Reset (well before the PHY number is clocked): no PHY claimed.
    win.cursor.set_sample(int(lc.bus_reset_sample or 0) + 1000)
    early = msgs()
    assert any("No PHY Selected" in t for t in early), early
    assert not any("PHY2" in t for t in early), f"claimed PHY2 before it was clocked: {early}"

    # After PhyStart, before audio: the PHY is now known.
    mid = int(lc.phystart_sample) + 1000
    assert mid < win._session.audio_start_sample
    win.cursor.set_sample(mid)
    after = msgs()
    assert any("PHY2 Selected" in t for t in after), after

    # Past audio start: real grid cells (no overlay message).
    win.cursor.set_sample(win._session.audio_start_sample + 5000)
    assert win._grid_view.item_count >= win._session.column_count


def test_grid_scroll_resets_so_new_content_is_visible():
    """Navigating from a wide/tall scrolled grid to a smaller scene (a narrower region,
    or the bring-up 'No PHY Selected' message drawn at the scene origin) must re-anchor
    the viewport at the top-left. Qt otherwise clamps the shrinking scene's scrollbars
    to the NEW maximum (not zero), leaving the fresh content scrolled off-screen — the
    'click twice to sync' bug."""
    app = QApplication.instance() or QApplication([])
    win = MainWindow()
    win.load_session(Session.from_demo(4000, cold_start=True))
    gv = win._grid_view
    gv.resize(600, 400)

    # Render a wide audio grid and scroll to its far corner.
    win.cursor.set_sample(win._session.audio_start_sample + 5000)
    app.processEvents()
    h, v = gv.horizontalScrollBar(), gv.verticalScrollBar()
    h.setValue(h.maximum()); v.setValue(v.maximum())
    app.processEvents()

    # Move into the bring-up: the message renders at the scene origin and must be visible.
    win.cursor.set_sample(int(win._session.link_control.bus_reset_sample or 0) + 1000)
    app.processEvents()
    vp = gv.mapToScene(gv.viewport().rect()).boundingRect()
    assert vp.left() <= 0 <= vp.right(), (
        f"message at scene x=0 scrolled out of view: viewport x [{vp.left()}, {vp.right()}]")
