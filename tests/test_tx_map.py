"""TX map: the physical data-line transition raster (Session.tx_raster) and its
Bus-Grid toggle. A where-is-real-data inspection that needs no config CSV — for
finding active columns / interval (vertical gap) structure and candidate SSP rows.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_tx_map.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np

from swi3s_studio.session import Session


def test_tx_raster_shape_and_labels():
    sess = Session.from_demo(64)
    r = sess.tx_raster(0, 64)
    cols = r["column_count"]
    assert r["tx"].shape == (64, cols)
    assert r["tx"].dtype == bool
    assert r["cds_col"] == 0
    assert len(r["row_labels"]) == 64
    assert r["row_labels"] == sorted(r["row_labels"])          # ascending real rows
    assert r["row_labels"][0] >= 0                             # clamped to the segment


def test_tx_raster_matches_edge_into_ui():
    """Each cell is 'the sampled bit differs from the previous UI' — a data edge in
    (clock_edge[ui-1], clock_edge[ui]] (the decoder's level[i]!=level[i-1] convention).
    Recompute that independently and cross-check — guards the origin/row_base/clip
    index math AND the off-by-one column alignment (a shift would light guard cols)."""
    sess = Session.from_demo(64)
    r = sess.tx_raster(0, 64)
    ce, de = sess.capture.clock_edges, sess.capture.data_edges
    # The raster draws at the OPERATIONAL geometry (the demo starts in safe-lock-2 and
    # widens 2 -> 8 -> 16 to the audio config), so recompute against that same geometry —
    # NOT segments[0], which is the narrow safe-lock preamble.
    origin, cols = sess._tx_geometry()
    checked = 0
    for i in range(0, 64, 5):
        for c in range(cols):
            ui = origin + i * cols + c
            if ui < 1 or ui >= ce.size:
                assert not bool(r["tx"][i][c])
                continue
            exp = int(np.searchsorted(de, ce[ui]) - np.searchsorted(de, ce[ui - 1])) > 0
            assert bool(r["tx"][i][c]) == exp, (i, c)
            checked += 1
    assert checked > 0


def test_gui_exposes_tx_map_buttons():
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    # TX map now lives on Bus Grid pane buttons (not the View menu).
    assert win._toggles_btn.isCheckable() and win._persist_btn.isCheckable()
    assert win._toggles_btn.text() == "Show Toggles"
    assert not win._persist_btn.isEnabled()          # only meaningful once toggles are on
    # Toggling with no capture loaded must not crash (just stores the flag + labels).
    win._toggles_btn.setChecked(True)
    assert win._tx_map is True
    assert win._toggles_btn.text() == "Hide Toggles"
    assert win._persist_btn.isEnabled()
    win._toggles_btn.setChecked(False)
    assert not win._persist_btn.isEnabled()
    # With a capture, toggles + persistence render the raster (and back) without error.
    win.load_demo()
    win._toggles_btn.setChecked(True)
    win._persist_btn.setChecked(True)                # persistence path
    win._persist_btn.setChecked(False)
    win._toggles_btn.setChecked(False)


def test_rows_to_draw_box():
    """The Rows-To-Draw box sets the Bus Grid raster/config height, clamped to 1..10240."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_demo()
    win._grid_rows_edit.setText("1"); win._on_grid_rows_edit()
    assert win._grid_rows == 1
    win._grid_rows_edit.setText("99999"); win._on_grid_rows_edit()
    assert win._grid_rows == 10240 and win._grid_rows_edit.text() == "10240"
    win._grid_rows_edit.setText("128"); win._on_grid_rows_edit()
    assert win._grid_rows == 128


def test_tx_persist_columns_region_scoped_and_lights_up():
    """Persistence summarises 'did this column EVER toggle' over the CONFIGURATION
    REGION under the cursor — scanning the whole region (not just the first
    Rows-To-Draw window), so a column that only transports deeper still lights up; and
    scoped to one region so distinct geometries (2col preamble vs 16col audio) don't
    overlay."""
    sess = Session.from_demo(64)
    # In the audio region (the operational 16-col segment), the transporting data columns
    # light up (more than just CDS). Anchor inside that segment directly — the demo's
    # link-control audio_start estimate isn't reliable for the plain (non-cold-start) demo.
    op = max(sess.segments, key=lambda s: int(s["column_count"]))
    samp = int(op["start_sample"]) + 5000
    lit, cols = sess.tx_persist_columns(samp)
    assert lit.shape == (cols,) and lit.dtype == bool
    assert int(lit.sum()) > 1, "persistence lit only CDS — data columns should light too"

    # Region scoping: a synthetic 2col preamble + 16col audio must report each region's
    # own column count, never overlay.
    sess.segments = [
        {"start_ui": 1,   "column_count": 2,  "start_sample": 0,   "end_sample": 400,      "row_base": 0},
        {"start_ui": 401, "column_count": 16, "start_sample": 400, "end_sample": 9_000_000, "row_base": 200},
    ]
    _lit0, c0 = sess.tx_persist_columns(100)        # inside the 2col preamble
    _lit1, c1 = sess.tx_persist_columns(500_000)    # inside the 16col audio
    assert c0 == 2 and c1 == 16, (c0, c1)


def test_persistence_relabels_rows_0_to_n():
    """With persistence on, every drawn row is the same collapsed pattern, so the
    gutter is numbered 0..N-1 (a pattern across N rows) rather than real bus rows."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_demo()
    win._grid_rows_edit.setText("8"); win._on_grid_rows_edit()
    # Anchor in the operational (audio) segment: the TX map only rasters once the cursor
    # is PAST the cold-start bring-up — a pre-audio cursor correctly shows 'No PHY
    # Selected' (no audio-mode column structure on the wire yet), not a raster.
    op = max(win._session.segments, key=lambda s: int(s["column_count"]))
    samp = int(op["start_sample"])
    seen = {}
    orig = win._grid_view.set_tx_raster
    win._grid_view.set_tx_raster = lambda r: (seen.update(r), orig(r))
    win._on_seek(samp)
    win._toggles_btn.setChecked(True); win._on_seek(samp)
    per_row = list(seen["row_labels"])
    win._persist_btn.setChecked(True); win._on_seek(samp)
    assert seen["row_labels"] == list(range(8)), seen["row_labels"]
    # per-row labels are real bus rows (ascending, may differ from 0..N-1 mid-capture)
    assert len(per_row) == 8


def test_tx_raster_start_row_and_total():
    """tx_raster takes a 0-based start row (window top) and tx_total_rows sizes the
    scrollbar; labels track the start row so the view can page the whole capture."""
    sess = Session.from_demo(64)
    total = sess.tx_total_rows()
    assert total >= 1
    r0 = sess.tx_raster(0, 8)
    assert r0["row_labels"] == list(range(8))
    r5 = sess.tx_raster(5, 8)
    assert r5["row_labels"] == list(range(5, 13))
    # A start row beyond the capture clamps to a valid (blank) window, no crash.
    rend = sess.tx_raster(total + 100, 4)
    assert len(rend["row_labels"]) == 4


def test_tx_virtual_scroll_pages_the_capture():
    """The TX scrollbar re-renders the window at its top row, covering every row."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_demo()
    # Cursor into the operational (audio) segment first — the TX map only rasters past
    # the cold-start bring-up (a pre-audio cursor shows 'No PHY Selected').
    op = max(win._session.segments, key=lambda s: int(s["column_count"]))
    win.cursor.set_sample(int(op["start_sample"]))
    total = win._session.tx_total_rows(win.cursor.sample)
    seen = {}
    orig = win._grid_view.set_tx_raster
    win._grid_view.set_tx_raster = lambda r: (seen.update(r), orig(r))
    win._grid_rows_edit.setText("16"); win._on_grid_rows_edit()
    win._toggles_btn.setChecked(True)                      # opens at the cursor's (audio) row 0
    assert win._tx_scroll.pageStep() == 16
    assert win._tx_scroll.maximum() == max(0, total - 16)
    assert seen["row_labels"][0] == 0                      # starts at row 0
    if total > 20:
        win._tx_scroll.setValue(10)
        assert win._tx_start_row == 10 and seen["row_labels"][0] == 10
    # Toggling off hides the scrollbar.
    win._toggles_btn.setChecked(False)
    assert not win._tx_scroll.isVisibleTo(win)


def test_tx_raster_uses_operational_geometry_not_preamble():
    """Regression: on a cold start segment 0 is the narrow slow-clock preamble (e.g.
    2 col). The TX map must render at the OPERATIONAL geometry (the audio config /
    decoder column_count), not force the whole raster to the preamble width — and its
    scrollbar extent (tx_total_rows) must use the same geometry."""
    sess = Session.from_demo(64)                              # column_count == 16
    # Simulate a cold-start segment list: a 2-col preamble then the 16-col audio.
    sess.segments = [
        {"start_ui": 1,   "column_count": 2,  "start_sample": 0,   "end_sample": 500,  "row_base": 0},
        {"start_ui": 401, "column_count": 16, "start_sample": 500, "end_sample": 9000, "row_base": 200},
    ]
    r = sess.tx_raster(0, 8)
    assert r["column_count"] == 16, r["column_count"]         # not the 2-col preamble
    assert r["tx"].shape == (8, 16)
    origin, cols = sess._tx_geometry()
    assert (origin, cols) == (401, 16)                        # anchored at the audio segment
    # tx_total_rows uses the same geometry (16-col rows), so the scrollbar agrees.
    avail = int(sess.capture.clock_edges.size) - 401
    assert sess.tx_total_rows() == max(1, (avail + 15) // 16)
    # Fallback: if no segment matches column_count, the widest wins.
    sess.column_count = 99
    assert sess._tx_geometry()[1] == 16


def test_show_toggles_opens_at_cursor_row():
    """Show Toggles opens the TX map at the CURSOR's row (not always row 0), in the
    raster's own geometry, and the scrollbar tracks it."""
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from swi3s_studio.ui.main_window import MainWindow
    win = MainWindow()
    win.load_demo()
    s = win._session
    origin, cols = s._tx_geometry()
    ce = s.capture.clock_edges
    # A sample about a third of the way through -> a mid TX-map row.
    target_row = max(1, s.tx_total_rows() // 3)
    ui = min(origin + target_row * cols, int(ce.size) - 1)
    win.cursor.set_sample(int(ce[ui]))
    want = s.tx_row_for_sample(int(ce[ui]))
    assert want > 0
    win._toggles_btn.setChecked(True)                 # fires _toggle_tx_map(True)
    assert win._tx_map
    assert win._tx_start_row == want                  # opened at the cursor row
    assert win._tx_scroll.value() == want             # scrollbar agrees
    win._toggles_btn.setChecked(False)
