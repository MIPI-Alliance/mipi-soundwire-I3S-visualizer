"""TX map: the physical data-line transition raster (Session.tx_raster) and its
Bus-Grid toggle. A where-is-real-data inspection that needs no config CSV — for
finding active columns / interval (vertical gap) structure and candidate SSP rows.

Run: QT_QPA_PLATFORM=offscreen PYTHONPATH=. python3 -m pytest tests/test_tx_map.py
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np
import pytest

from swi3s_studio.session import Session

pytestmark = pytest.mark.gui


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
    [clock_edge[ui-1], clock_edge[ui]) (the decoder's level[i]!=level[i-1] convention).
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
    # TX map lives on Bus Grid pane buttons.
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
    op = max(win._session.segments, key=lambda s: int(s["column_count"]))
    win.cursor.set_sample(int(op["start_sample"]) + 5000)
    seen = {}
    orig = win._grid_view.set_tx_raster
    win._grid_view.set_tx_raster = lambda r: (seen.update(r), orig(r))
    win._toggles_btn.setChecked(True)
    assert seen.get("tx") is not None and seen["tx"].any(), "toggles drew no raster"
    seen.clear()
    win._persist_btn.setChecked(True)                # persistence path
    assert seen.get("tx") is not None, "persistence drew no raster"
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
    for low in ("0", "-5"):
        win._grid_rows_edit.setText(low); win._on_grid_rows_edit()
        assert win._grid_rows == 1 and win._grid_rows_edit.text() == "1", low
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
    deep = Session.from_demo(64)
    ce = deep.capture.clock_edges
    deep.segments = [{"start_ui": 1, "column_count": 16, "start_sample": 0,
                      "end_sample": int(ce[-1]) + 1, "row_base": 0}]
    # One data edge in row 1000, column 5: far beyond any Rows-To-Draw window.
    deep.capture.data_edges = np.array([ce[1 + 16 * 1000 + 5] - 1],
                                       dtype=deep.capture.data_edges.dtype)
    assert np.flatnonzero(deep.tx_persist_columns(int(ce[10]))[0]).tolist() == [5]

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
    # Anchor mid-capture, in the operational segment: there the real bus-row labels start
    # above 0, so only relabelling can make the gutter read 0..N-1.
    s = win._session
    origin, cols = s._tx_geometry()
    ui = min(origin + max(1, s.tx_total_rows() // 3) * cols, int(s.capture.clock_edges.size) - 1)
    samp = int(s.capture.clock_edges[ui])
    seen = {}
    orig = win._grid_view.set_tx_raster
    win._grid_view.set_tx_raster = lambda r: (seen.update(r), orig(r))
    win._on_seek(samp)
    seen.clear(); win._toggles_btn.setChecked(True)
    per_row = list(seen["row_labels"])
    assert len(per_row) == 8 and per_row != list(range(8)), per_row
    seen.clear(); win._persist_btn.setChecked(True)
    assert seen["row_labels"] == list(range(8)), seen["row_labels"]


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
    assert total > 20, total
    assert win._tx_scroll.isVisibleTo(win)
    assert win._tx_scroll.pageStep() == 16
    assert win._tx_scroll.maximum() == max(0, total - 16)
    assert seen["row_labels"][0] == 0                      # starts at row 0
    win._tx_scroll.setValue(10)
    assert win._tx_start_row == 10 and seen["row_labels"][0] == 10
    # Toggling off hides the scrollbar.
    win._toggles_btn.setChecked(False)
    assert not win._tx_scroll.isVisibleTo(win)


def test_tx_raster_uses_operational_geometry_not_preamble():
    """On a cold start segment 0 is the narrow slow-clock preamble (e.g.
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


@pytest.mark.parametrize("cold_start", [False, True])
def test_tx_map_on_phy3_dlv_regions(cold_start):
    """On a PHY3 (DLV) capture the TX map counts rows by the recovered Row Sync Points:
    in each region the persistence summary and the raster light exactly the columns the
    decoded grid occupies plus the CDS column, the raster spans the region's bus rows, and
    the cursor's TX-map row is its bus row less the region's first."""
    sess = Session.from_demo(32, phy=3, cold_start=cold_start)
    assert sess.is_dlv and len(sess.segments) == 2
    nrsp = int(sess._recovered_row_syncs().size)
    for i, seg in enumerate(sess.segments):
        samp = int(seg["start_sample"]) + 10
        lit, cols = sess.tx_persist_columns(samp)
        assert cols == int(seg["column_count"])
        cds = sess.cds_column_at(samp)
        occupied = {int(c["col"]) for c in sess.grid_cells_at(samp + 2000, rows=64)
                    if not c.get("is_cds")}
        assert set(np.flatnonzero(lit).tolist()) == occupied | {cds}, (i, lit.astype(int))
        row_hi = (int(sess.segments[i + 1]["row_base"]) if i + 1 < len(sess.segments)
                  else nrsp)
        total = sess.tx_total_rows(samp)
        assert total == row_hi - int(seg["row_base"])
        r = sess.tx_raster(0, total, samp)
        assert r["tx"].shape == (total, cols) and r["cds_col"] == cds
        assert np.array_equal(r["tx"].any(axis=0), lit)
        assert r["tx"][:, 0].all()                     # every row opens on its RSP edge
        seg_end = (int(sess.segments[i + 1]["start_sample"]) if i + 1 < len(sess.segments)
                   else int(sess.capture.clock_edges[-1]))
        later = min(samp + 50 * int(np.median(np.diff(sess._recovered_row_syncs()))),
                    seg_end - 1)                      # a later sample inside this region
        assert samp < later < seg_end
        assert (sess.tx_row_for_sample(later)
                == sess.bus_row_for_sample(later) - int(seg["row_base"]))


def test_tx_persist_columns_clamps_region_to_clock_edges():
    """A region whose next segment starts past the last clock edge (a truncated capture)
    is scanned to the capture end."""
    sess = Session.from_demo(64)
    nui = int(sess.capture.clock_edges.size)
    op = max(sess.segments, key=lambda s: int(s["column_count"]))
    samp = int(op["start_sample"]) + 5000
    full, cols = sess.tx_persist_columns(samp)
    sess._tx_persist_cache = {}
    sess.segments = [s for s in sess.segments if int(s["start_ui"]) <= int(op["start_ui"])] + [
        {"start_ui": nui + 1000, "column_count": cols, "start_sample": 10**12,
         "end_sample": 10**12, "row_base": 10**9}]
    assert sess.tx_persist_key(samp)[2] == nui
    lit, c2 = sess.tx_persist_columns(samp)
    assert c2 == cols and np.array_equal(lit, full)


def test_tx_raster_stops_at_its_regions_end():
    """A region's raster lights no cell from the next region's first UI on, though the
    data line keeps toggling there; before it every cell is the edge-into-UI rule."""
    sess = Session.from_demo(64)
    seg = sess.segments[1]                              # 8 columns, then the 16-column region
    sample = int(seg["start_sample"]) + 1000
    origin, cols, end_ui = sess._tx_bounds(sample)
    assert cols == int(seg["column_count"]) and end_ui < sess.capture.clock_edges.size
    nrows = (end_ui - origin) // cols + 8
    tx = sess.tx_raster(0, nrows, sample)["tx"]
    ce, de = sess.capture.clock_edges, sess.capture.data_edges
    ui = origin + np.arange(nrows)[:, None] * cols + np.arange(cols)[None, :]
    inside = (ui >= 1) & (ui < end_ui)
    uic = np.clip(ui, 1, ce.size - 1)
    rule = (np.searchsorted(de, ce[uic]) - np.searchsorted(de, ce[uic - 1])) > 0
    assert rule[~inside].any(), "test premise: the line toggles past the region's end"
    assert not tx[~inside].any()
    assert np.array_equal(tx[inside], rule[inside])
